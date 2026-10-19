"""
Keysight 34461A - Messprogramm mit Browser-Oberflaeche
=======================================================

Fasst die bisherigen Keysight-Skripte in einem Programm zusammen. Beim
Start oeffnet sich im Browser ein Menue, in dem Verbindung, Messfunktion,
Messbereich, Datei und der Modus eingestellt werden:

  - Probe-Hold (DUT-Serie): wie keysight_dut_probehold.py - erkennt einen
    stabilen Messwert automatisch, speichert ihn, piept und wartet auf den
    Kontaktwechsel zum naechsten DUT. Beide Haende bleiben frei.
  - Messwerte aufzeichnen: wie keysight_trend_logger.py - misst im festen
    Intervall, zeichnet den Verlauf live und speichert jeden Wert.
  - Einzelmessungen: wie beim Resistomat - der aktuelle Wert wird laufend
    angezeigt und mit Enter uebernommen.

Die Oberflaeche steht in keysight_34461a.html (gleicher Aufbau wie
Resistomat_V2.html) und muss im selben Ordner liegen. Ein Browser kann aber
nicht selbst per LAN mit dem Geraet sprechen, deshalb laeuft dieses Skript
als kleiner lokaler Server (nur 127.0.0.1) und erledigt die Kommunikation
per pyvisa. Messung und Speichern laufen im Skript, auch wenn der
Browser-Tab im Hintergrund liegt oder geschlossen wird; die Seite kann
jederzeit wieder geoeffnet werden.

CSV: Semikolon + Dezimalkomma, damit deutsches Excel die Datei direkt
richtig oeffnet. Eine vorhandene Datei wird weitergefuehrt.

Voraussetzungen:
    pip install pyvisa pyvisa-py
"""

import json
import queue
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyvisa


HOST = '127.0.0.1'
PORT = 8765
CONFIG_PATH = Path.home() / '.keysight_34461a_config.json'
OVERLOAD = 9.0e37       # Geraet meldet Ueberlast als +/-9.9E37
MAX_ERRORS = 3          # aufeinanderfolgende Lesefehler bis "Verbindung verloren"
IDLE_POLL = 0.3         # s, Abfrage fuer die Live-Anzeige, wenn nichts aufgezeichnet wird
PAGE_PATH = Path(__file__).with_name('keysight_34461a.html')   # Oberflaeche, liegt neben dem Skript
LOCK_MSG = ('Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen - '
            'Werte bleiben im Speicher und werden nachgeschrieben.')

# Messfunktionen: CONF-Name -> Anzeige, Messbereiche [SCPI-Wert, Text],
# Anzeigeeinheiten [Name, Faktor zum Geraetewert], NPLC einstellbar
FUNCTIONS = {
    'VOLT:DC': {'label': 'DC-Spannung', 'nplc': True,
                'ranges': [['0.1', '100 mV'], ['1', '1 V'], ['10', '10 V'], ['100', '100 V'], ['1000', '1000 V']],
                'units': [['mV', 1e3], ['V', 1]]},
    'VOLT:AC': {'label': 'AC-Spannung', 'nplc': False,
                'ranges': [['0.1', '100 mV'], ['1', '1 V'], ['10', '10 V'], ['100', '100 V'], ['750', '750 V']],
                'units': [['mV', 1e3], ['V', 1]]},
    'CURR:DC': {'label': 'DC-Strom', 'nplc': True,
                'ranges': [['1E-4', '100 µA'], ['1E-3', '1 mA'], ['1E-2', '10 mA'], ['0.1', '100 mA'], ['1', '1 A'], ['3', '3 A']],
                'units': [['mA', 1e3], ['µA', 1e6], ['A', 1]]},
    'CURR:AC': {'label': 'AC-Strom', 'nplc': False,
                'ranges': [['1E-4', '100 µA'], ['1E-3', '1 mA'], ['1E-2', '10 mA'], ['0.1', '100 mA'], ['1', '1 A'], ['3', '3 A']],
                'units': [['mA', 1e3], ['µA', 1e6], ['A', 1]]},
    'RES':     {'label': 'Widerstand 2-Leiter', 'nplc': True,
                'ranges': [['100', '100 Ω'], ['1E3', '1 kΩ'], ['1E4', '10 kΩ'], ['1E5', '100 kΩ'], ['1E6', '1 MΩ'], ['1E7', '10 MΩ'], ['1E8', '100 MΩ']],
                'units': [['Ω', 1], ['kΩ', 1e-3], ['MΩ', 1e-6]]},
    'FRES':    {'label': 'Widerstand 4-Leiter', 'nplc': True,
                'ranges': [['100', '100 Ω'], ['1E3', '1 kΩ'], ['1E4', '10 kΩ'], ['1E5', '100 kΩ'], ['1E6', '1 MΩ'], ['1E7', '10 MΩ'], ['1E8', '100 MΩ']],
                'units': [['Ω', 1], ['mΩ', 1e3], ['kΩ', 1e-3]]},
    'FREQ':    {'label': 'Frequenz', 'nplc': False, 'ranges': [],
                'units': [['Hz', 1], ['kHz', 1e-3]]},
    'CAP':     {'label': 'Kapazität', 'nplc': False,
                'ranges': [['1E-9', '1 nF'], ['1E-8', '10 nF'], ['1E-7', '100 nF'], ['1E-6', '1 µF'], ['1E-5', '10 µF'], ['1E-4', '100 µF']],
                'units': [['nF', 1e9], ['µF', 1e6], ['pF', 1e12]]},
}
NPLC_VALUES = ['0.02', '0.2', '1', '10', '100']
HEADERS = {
    'probe': ['Datum/Zeit', 'DUT', 'Messwert', 'Einheit'],
    'trend': ['Datum/Zeit', 'Zeit_s', 'Messwert', 'Einheit'],
    'manual': ['Datum/Zeit', 'Nr', 'Messwert', 'Einheit'],
}


# ---------------------------------------------------------
# Konfiguration merken (letzte Einstellungen im Menue)
# ---------------------------------------------------------
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(data):
    try:
        CONFIG_PATH.write_text(json.dumps(data), encoding='utf-8')
    except OSError:
        pass


# ---------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------
def timestamp():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def csv_num(v, fmt='.7g'):
    return format(v, fmt).replace('.', ',')


def resolve_path(text):
    path = Path(text.strip()).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path


def count_data_lines(path):
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            return max(0, sum(1 for line in f if line.strip()) - 1)
    except OSError:
        return 0


class CsvLog:
    """CSV-Datei, an die Zeilen angehaengt werden. Ist die Datei gesperrt
    (z.B. in Excel geoeffnet), bleiben die Zeilen in 'pending' und werden
    beim naechsten flush() nachgeschrieben."""

    def __init__(self, path, header):
        self.path = path
        self.pending = []
        self.existing = 0
        if not path.exists() or path.stat().st_size == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            # BOM, damit Excel Umlaute und Einheiten (µ, Ω) richtig anzeigt
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write(';'.join(header) + '\r\n')
        else:
            self.existing = count_data_lines(path)
            with open(path, 'a', encoding='utf-8'):
                pass  # wirft PermissionError, wenn die Datei gesperrt ist

    def append(self, fields):
        self.pending.append(';'.join(fields) + '\r\n')
        return self.flush()

    def flush(self):
        if not self.pending:
            return True
        try:
            with open(self.path, 'a', encoding='utf-8', newline='') as f:
                f.write(''.join(self.pending))
            self.pending.clear()
            return True
        except OSError:
            return False

    def drop_last(self):
        """Entfernt die zuletzt angehaengte Zeile. False, wenn gesperrt."""
        if self.pending:
            self.pending.pop()
            return True
        try:
            data = self.path.read_bytes().rstrip(b'\r\n')
            cut = data.rfind(b'\n')
            if cut < 0:
                return True
            self.path.write_bytes(data[:cut + 1])
            return True
        except OSError:
            return False


# ---------------------------------------------------------
# Geraet
# ---------------------------------------------------------
def open_instrument(ip, conn):
    # Erst Raw-Socket (Port 5025) versuchen, bei Fehlschlag auf
    # VXI-11 ('inst0::INSTR') zurueckfallen.
    resources = {
        'socket': [f'TCPIP0::{ip}::5025::SOCKET'],
        'vxi11': [f'TCPIP0::{ip}::inst0::INSTR'],
    }
    resources['auto'] = resources['socket'] + resources['vxi11']

    rm = pyvisa.ResourceManager('@py')
    fehler = []
    for resource in resources[conn]:
        inst = None
        try:
            inst = rm.open_resource(resource)
            inst.timeout = 10000
            inst.read_termination = '\n'
            inst.write_termination = '\n'
            idn = inst.query('*IDN?').strip()
            return inst, resource, idn
        except Exception as e:
            fehler.append(f'{resource}: {e}')
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
    raise ConnectionError('Geraet nicht erreichbar - IP/LAN pruefen.\n' + '\n'.join(fehler))


def device_errors(inst):
    errors = []
    for _ in range(10):
        e = inst.query('SYST:ERR?').strip()
        if e.startswith('+0') or e.startswith('0'):
            break
        errors.append(e)
    return errors


def configure_instrument(inst, cfg):
    """Stellt Messfunktion, Bereich und NPLC ein und liest zur Kontrolle zurueck."""
    func = FUNCTIONS[cfg['function']]
    inst.write('*CLS')
    if func['ranges']:
        inst.write(f"CONF:{cfg['function']} {cfg['range']}")
    else:
        inst.write(f"CONF:{cfg['function']}")
    if func['nplc'] and cfg['nplc']:
        inst.write(f"SENS:{cfg['function']}:NPLC {cfg['nplc']}")
    for line in cfg['extra']:
        inst.write(line)
    errors = device_errors(inst)
    if errors:
        raise RuntimeError('Geraet meldet Fehler: ' + ' | '.join(errors))

    info = [func['label']]
    if func['ranges']:
        label = dict(func['ranges']).get(cfg['range'], 'Automatik')
        info.append('Bereich ' + label)
    if func['nplc']:
        nplc = inst.query(f"SENS:{cfg['function']}:NPLC?").strip()
        info.append(f'NPLC {float(nplc):g}')
    info.append('CONF? ' + inst.query('CONF?').strip().strip('"'))
    return info


# ---------------------------------------------------------
# Messsitzung (laeuft im Hintergrund-Thread)
# ---------------------------------------------------------
def check_config(data):
    def num(key, lo, hi, integer=False):
        try:
            v = int(data[key]) if integer else float(str(data[key]).replace(',', '.'))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'Ungueltige Eingabe: {key}')
        if not lo <= v <= hi:
            raise ValueError(f'{key} muss zwischen {lo} und {hi} liegen.')
        return v

    cfg = dict(data)
    if cfg.get('mode') not in HEADERS:
        raise ValueError('Unbekannter Modus.')
    if not str(cfg.get('ip', '')).strip():
        raise ValueError('Bitte IP-Adresse eingeben.')
    cfg['ip'] = cfg['ip'].strip()
    if cfg.get('conn') not in ('auto', 'socket', 'vxi11'):
        cfg['conn'] = 'auto'
    func = FUNCTIONS.get(cfg.get('function'))
    if func is None:
        raise ValueError('Unbekannte Messfunktion.')
    if cfg.get('range') not in [r[0] for r in func['ranges']]:
        cfg['range'] = 'AUTO'
    if cfg.get('nplc') not in NPLC_VALUES:
        cfg['nplc'] = ''
    units = dict(func['units'])
    if cfg.get('unit') not in units:
        cfg['unit'] = func['units'][0][0]
    cfg['factor'] = units[cfg['unit']]
    extra = [l.strip() for l in str(cfg.get('extra', '')).splitlines() if l.strip()]
    if any('?' in l for l in extra):
        raise ValueError('Zusaetzliche SCPI-Befehle duerfen keine Abfragen (?) enthalten.')
    cfg['extra'] = extra
    if not str(cfg.get('path', '')).strip():
        raise ValueError('Bitte Speicherort fuer die CSV-Datei angeben.')

    if cfg['mode'] == 'probe':
        cfg['target'] = num('dut_count', 1, 100000, True)
        cfg['tol'] = num('tol', 1e-12, 1e12)
        cfg['stable_n'] = num('stable_n', 2, 100, True)
        cfg['reset'] = num('reset', 0, 1e12)
        cfg['idle'] = num('idle', 0, 1e12)
        cfg['poll'] = num('poll', 0.05, 60)
        if cfg.get('beep') not in ('pc', 'device', 'both', 'off'):
            cfg['beep'] = 'pc'
    elif cfg['mode'] == 'trend':
        cfg['target'] = num('trend_count', 0, 10000000, True)
        cfg['interval'] = num('interval', 0.1, 86400)
    else:
        cfg['target'] = num('manual_count', 1, 100000, True)
    return cfg


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.inst = None
        self.worker = None
        self.stop_worker = threading.Event()
        self.cmds = queue.Queue()
        self.rows_ver = 0
        self._reset({})

    def _reset(self, cfg):
        self.cfg = cfg
        self.mode = cfg.get('mode', '')
        self.connected = False
        self.lost = False
        self.info = []
        self.running = False
        self.phase = ''           # Probe-Hold: 'stabil' | 'wechsel'
        self.window = []
        self.ref = None
        self.current = None
        self.overload = False
        self.rows = []            # [nr, wert, t_s, zeitstempel]
        self.rows_ver += 1
        self.t0 = time.time()
        self.next_tick = 0.0
        self.status = ''
        self.status_kind = ''
        self.event_seq = 0
        self.event_kind = ''
        self.log = None

    # ---------- Hilfen (nur mit self.lock aufrufen) ----------
    def _set_status(self, text, kind=''):
        self.status, self.status_kind = text, kind

    def _event(self, kind):
        self.event_seq += 1
        self.event_kind = kind
        beep = self.cfg.get('beep')
        if self.mode == 'probe' and beep in ('device', 'both'):
            self.cmds.put('SYST:BEEP')
            if kind == 'done':
                self.cmds.put('SYST:BEEP')

    def _done(self):
        target = self.cfg.get('target', 0)
        return target > 0 and len(self.rows) >= target

    def _fields(self, nr, v, t, ts):
        second = csv_num(t, '.3f') if self.mode == 'trend' else str(nr)
        return [ts, second, csv_num(v), self.cfg['unit']]

    def _record(self, v, t):
        nr, ts = len(self.rows) + 1, timestamp()
        ok = self.log.append(self._fields(nr, v, t, ts))
        self.rows.append([nr, v, round(t, 3), ts])
        if self._done():
            self.running = False
            self.phase = ''
            self._event('done')
            self._set_status('Alle Messungen erfasst.' if ok else LOCK_MSG, 'ok' if ok else 'err')
        else:
            if self.mode == 'probe':
                self._event('ok')
            self._set_status('Gespeichert.' if ok else LOCK_MSG, 'ok' if ok else 'err')

    def _probe_step(self, v):
        c = self.cfg
        contact = v is not None and (c['idle'] <= 0 or abs(v) >= c['idle'])
        if self.phase == 'stabil':
            if not contact:
                self.window = []
                return
            self.window.append(v)
            del self.window[:-c['stable_n']]
            if len(self.window) == c['stable_n'] and max(self.window) - min(self.window) < c['tol']:
                value = sum(self.window) / len(self.window)
                self.window = []
                self.ref = value
                self._record(value, time.time() - self.t0)
                if self.running:
                    self.phase = 'wechsel'
        elif self.phase == 'wechsel':
            if not contact or abs(v - self.ref) > c['reset']:
                self.phase = 'stabil'
                self.window = []

    # ---------- Aktionen aus der Oberflaeche ----------
    def connect(self, data):
        cfg = check_config(data)
        path = resolve_path(cfg['path'])
        self.disconnect({})
        inst, resource, idn = open_instrument(cfg['ip'], cfg['conn'])
        try:
            info = configure_instrument(inst, cfg)
            log = CsvLog(path, HEADERS[cfg['mode']])
        except PermissionError:
            inst.close()
            raise RuntimeError(f'Datei {path} ist gesperrt (z.B. in Excel geoeffnet). Bitte schliessen.')
        except Exception:
            inst.close()
            raise
        save_config({k: v for k, v in data.items()})
        with self.lock:
            self._reset(cfg)
            self.inst = inst
            self.log = log
            self.connected = True
            self.info = info
            self.idn = idn
            self.resource = resource
            self.path = str(path)
            self._set_status(f'{log.existing} vorhandene Zeilen in der Datei, neue werden angehaengt.'
                             if log.existing else 'Neue Datei angelegt.', 'ok')
        self.stop_worker = threading.Event()
        self.cmds = queue.Queue()
        self.worker = threading.Thread(target=self._run, args=(inst, self.stop_worker), daemon=True)
        self.worker.start()
        return {'ok': True}

    def disconnect(self, data):
        self.stop_worker.set()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=15)
        self.worker = None
        with self.lock:
            if self.log is not None:
                self.log.flush()
            if self.inst is not None:
                try:
                    self.inst.close()
                except Exception:
                    pass
            self.inst = None
            self.connected = False
            self.running = False
        return {'ok': True}

    def start(self, data):
        with self.lock:
            if not self.connected or self.mode == 'manual' or self.running or self._done():
                return {'ok': False}
            now = time.time()
            if self.mode == 'trend':
                # Fortsetzen: Zeitachse laeuft ab dem letzten Wert weiter
                self.t0 = now - (self.rows[-1][2] + self.cfg['interval'] if self.rows else 0)
                self.next_tick = now
                self._set_status('Aufzeichnung laeuft ...', 'ok')
            else:
                # Nach einer Pause zuerst den Kontaktwechsel abwarten,
                # damit das letzte DUT nicht doppelt erfasst wird
                self.phase = 'wechsel' if self.rows else 'stabil'
                self.ref = self.rows[-1][1] if self.rows else None
                self.window = []
                self._set_status('Messung laeuft - Pruefspitzen ansetzen.', 'ok')
            self.running = True
        return {'ok': True}

    def stop(self, data):
        with self.lock:
            self.running = False
            self.phase = ''
            ok = self.log.flush() if self.log else True
            self._set_status('Gestoppt - Datei gespeichert.' if ok else LOCK_MSG, 'ok' if ok else 'err')
        return {'ok': True}

    def confirm(self, data):
        with self.lock:
            if not self.connected or self.mode != 'manual' or self._done():
                return {'ok': False}
            if self.current is None:
                self._set_status('Kein gueltiger Messwert (Ueberlast?).', 'err')
                return {'ok': False}
            nr, t, ts = len(self.rows) + 1, time.time() - self.t0, timestamp()
            if not self.log.append(self._fields(nr, self.current, t, ts)):
                self.log.pending.pop()
                self._set_status('Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen und erneut versuchen.', 'err')
                return {'ok': False}
            self.rows.append([nr, self.current, round(t, 3), ts])
            self._set_status('Alle Messungen erfasst.' if self._done() else 'Gespeichert.', 'ok')
        return {'ok': True}

    def undo(self, data):
        """Einzelmessung: letzten Wert durch den aktuellen ersetzen.
        Probe-Hold: letztes DUT verwerfen und neu messen."""
        with self.lock:
            if not self.connected or self.mode not in ('probe', 'manual') or not self.rows:
                return {'ok': False}
            if self.mode == 'manual' and self.current is None:
                self._set_status('Kein gueltiger Messwert (Ueberlast?).', 'err')
                return {'ok': False}
            if not self.log.drop_last():
                self._set_status('Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen und erneut versuchen.', 'err')
                return {'ok': False}
            dropped = self.rows.pop()
            self.rows_ver += 1
            if self.mode == 'manual':
                nr, t, ts = dropped[0], time.time() - self.t0, timestamp()
                ok = self.log.append(self._fields(nr, self.current, t, ts))
                self.rows.append([nr, self.current, round(t, 3), ts])
                self._set_status('Letzter Wert ueberschrieben.' if ok else LOCK_MSG, 'ok' if ok else 'err')
            else:
                # Pruefspitzen loesen und neu ansetzen, dann wird das DUT neu erfasst
                self.ref = dropped[1]
                self.window = []
                self.phase = 'wechsel'
                self.running = True
                self._set_status(f'DUT {dropped[0]} verworfen - Pruefspitzen neu ansetzen.', 'ok')
        return {'ok': True}

    def state(self, since, ver):
        with self.lock:
            if ver != self.rows_ver or since > len(self.rows):
                since = 0
            c = self.cfg
            spread = max(self.window) - min(self.window) if len(self.window) > 1 else None
            return {
                'connected': self.connected, 'lost': self.lost, 'mode': self.mode,
                'running': self.running, 'done': self._done() if self.connected or self.lost else False,
                'phase': self.phase, 'fill': len(self.window), 'spread': spread,
                'stable_n': c.get('stable_n'), 'tol': c.get('tol'),
                'interval': c.get('interval'), 'target': c.get('target', 0),
                'unit': c.get('unit', ''), 'beep_pc': c.get('beep') in ('pc', 'both'),
                'current': self.current, 'overload': self.overload,
                'count': len(self.rows), 'rows': self.rows[since:], 'rows_from': since,
                'rows_ver': self.rows_ver, 'status': self.status, 'status_kind': self.status_kind,
                'event_seq': self.event_seq, 'event_kind': self.event_kind,
                'info': self.info, 'path': getattr(self, 'path', ''),
                'save_ok': not (self.log and self.log.pending),
            }

    # ---------- Hintergrund-Thread ----------
    def _run(self, inst, stop):
        errors = 0
        next_read = 0.0
        while not stop.is_set():
            with self.lock:
                mode, running = self.mode, self.running
                poll = self.cfg.get('poll', IDLE_POLL) if mode == 'probe' else IDLE_POLL
                if self.log.pending and self.log.flush() and self.status == LOCK_MSG:
                    self._set_status('Datei wieder frei - Werte nachgeschrieben.', 'ok')
            try:
                while True:
                    inst.write(self.cmds.get_nowait())
            except queue.Empty:
                pass
            except Exception:
                pass  # Fehler zeigt sich beim naechsten READ?

            if mode == 'trend' and running:
                wait = self.next_tick - time.time()
            else:
                wait = next_read - time.time()
            if wait > 0:
                time.sleep(min(wait, 0.05))
                continue
            next_read = time.time() + poll

            try:
                raw = float(inst.query('READ?'))
                errors = 0
            except Exception as e:
                errors += 1
                with self.lock:
                    if errors >= MAX_ERRORS:
                        self.connected = False
                        self.lost = True
                        self.running = False
                        self.current = None
                        self._event('err')
                        self._set_status(f'Verbindung zum Geraet verloren ({e}).', 'err')
                    else:
                        self._set_status(f'Lesefehler: {e}', 'err')
                if errors >= MAX_ERRORS:
                    break
                try:
                    inst.clear()
                except Exception:
                    pass
                continue

            now = time.time()
            with self.lock:
                if stop.is_set():
                    break
                v = None if abs(raw) >= OVERLOAD else raw * self.cfg['factor']
                self.current = v
                self.overload = v is None
                if self.mode == 'trend' and self.running:
                    self.next_tick = max(self.next_tick + self.cfg['interval'], now)
                    if v is None:
                        self._set_status('Ueberlast - Wert nicht gespeichert.', 'err')
                    else:
                        self._record(v, now - self.t0)
                elif self.mode == 'probe' and self.running:
                    self._probe_step(v)


SESSION = Session()
_browse_lock = threading.Lock()


def browse(data):
    """Speichern-Dialog von Windows fuer die CSV-Datei."""
    if not _browse_lock.acquire(blocking=False):
        return {'ok': False, 'err': 'Der Dialog ist bereits offen.'}
    try:
        import tkinter as tk
        from tkinter import filedialog
        cur = str(data.get('path') or '').strip()
        cur_path = resolve_path(cur) if cur else Path.home() / 'messungen.csv'
        initialdir = cur_path.parent if cur_path.parent.is_dir() else Path.home()
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.asksaveasfilename(
            parent=root, title='CSV-Datei waehlen/erstellen',
            defaultextension='.csv', filetypes=[('CSV-Datei (Excel)', '*.csv')],
            initialdir=str(initialdir), initialfile=cur_path.name, confirmoverwrite=False,
        )
        root.destroy()
        if not path:
            return {'ok': True, 'path': ''}
        return {'ok': True, 'path': str(Path(path)), 'existing': count_data_lines(path) if Path(path).exists() else -1}
    except Exception as e:
        return {'ok': False, 'err': f'Dialog nicht verfuegbar ({e}) - Pfad bitte eintippen.'}
    finally:
        _browse_lock.release()


def file_info(data):
    path = resolve_path(str(data.get('path') or ''))
    return {'ok': True, 'path': str(path), 'existing': count_data_lines(path) if path.exists() else -1}


# ---------------------------------------------------------
# Lokaler Webserver
# ---------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host_ok(self):
        # Schutz gegen fremde Webseiten (DNS-Rebinding)
        port = self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8', code)

    def do_GET(self):
        if not self._host_ok():
            return self._json({'ok': False}, 403)
        url = urlparse(self.path)
        if url.path == '/':
            self._send(PAGE_PATH.read_bytes(), 'text/html; charset=utf-8')
        elif url.path == '/api/meta':
            self._json({'functions': FUNCTIONS, 'nplc': NPLC_VALUES, 'config': load_config()})
        elif url.path == '/api/state':
            qs = parse_qs(url.query)
            try:
                since = int(qs.get('since', ['0'])[0])
                ver = int(qs.get('ver', ['-1'])[0])
            except ValueError:
                since, ver = 0, -1
            self._json(SESSION.state(since, ver))
        else:
            self._json({'ok': False}, 404)

    def do_POST(self):
        # Nur JSON von der eigenen Seite annehmen (fremde Seiten koennen
        # diesen Content-Type nicht ohne Rueckfrage senden)
        if not self._host_ok() or not self.headers.get('Content-Type', '').startswith('application/json'):
            return self._json({'ok': False}, 403)
        routes = {
            '/api/connect': SESSION.connect, '/api/disconnect': SESSION.disconnect,
            '/api/start': SESSION.start, '/api/stop': SESSION.stop,
            '/api/confirm': SESSION.confirm, '/api/undo': SESSION.undo,
            '/api/browse': browse, '/api/fileinfo': file_info, '/api/quit': quit_program,
        }
        fn = routes.get(urlparse(self.path).path)
        if fn is None:
            return self._json({'ok': False}, 404)
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
            self._json(fn(data))
        except Exception as e:
            self._json({'ok': False, 'err': str(e)})


SERVER = None


def quit_program(data):
    SESSION.disconnect({})
    threading.Thread(target=SERVER.shutdown, daemon=True).start()
    return {'ok': True}


def main():
    global SERVER
    if not PAGE_PATH.exists():
        print(f'{PAGE_PATH.name} fehlt - muss im selben Ordner wie dieses Skript liegen.')
        return
    try:
        SERVER = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        SERVER = ThreadingHTTPServer((HOST, 0), Handler)   # Port belegt -> freien nehmen
    url = f'http://{HOST}:{SERVER.server_port}/'
    print('Keysight 34461A - Messprogramm')
    print(f'Oberflaeche: {url}')
    print('Dieses Fenster offen lassen. Beenden mit "Programm beenden" im Browser oder Strg+C.')
    webbrowser.open(url)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.disconnect({})
        SERVER.server_close()
        print('Beendet.')




if __name__ == '__main__':
    main()
