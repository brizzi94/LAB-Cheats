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

Die Oberflaeche ist aufgebaut wie Resistomat_V2.html. Ein Browser kann aber
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
            self._send(PAGE.encode('utf-8'), 'text/html; charset=utf-8')
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


# ---------------------------------------------------------
# Oberflaeche (gleicher Aufbau wie Resistomat_V2.html)
# ---------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keysight 34461A - Messung</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --text:#1c1e21; --muted:#6b7280; --accent:#1a6fd4; --border:#d5d9e0; --ok:#1a8a4a; --err:#c0392b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181c; --card:#20242a; --text:#e8eaed; --muted:#9aa3ad; --accent:#4c9aff; --border:#343a43; --ok:#4cc27f; --err:#ff6b5b; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--text); display:flex; justify-content:center; padding:24px; }
  .wrap { width:100%; max-width:680px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }
  h1 { font-size:18px; margin:0 0 16px; }
  h2 { font-size:14px; margin:22px 0 2px; padding-top:14px; border-top:1px solid var(--border); }
  label { display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }
  input[type=number], input[type=text], select, textarea { width:100%; padding:8px; font-size:15px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font-family:inherit; }
  textarea { font-family:Consolas, monospace; font-size:13px; min-height:52px; resize:vertical; }
  button { font-family:inherit; font-size:15px; padding:10px 16px; border-radius:8px; border:1px solid var(--border); background:var(--card); color:var(--text); cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-size:17px; padding:12px 20px; }
  button.stop { background:var(--err); border-color:var(--err); color:#fff; font-size:17px; padding:12px 20px; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .row { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:0 12px; }
  .modes { display:grid; grid-template-columns:repeat(3, 1fr); gap:10px; margin-top:6px; }
  .modes label { margin:0; padding:12px; border:1px solid var(--border); border-radius:10px; cursor:pointer; color:var(--text); background:var(--bg); }
  .modes label:has(input:checked) { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
  .modes input { margin:0 6px 0 0; }
  .modes strong { font-size:14px; }
  .modes span.d { display:block; margin-top:4px; font-size:12px; color:var(--muted); line-height:1.35; }
  @media (max-width:560px) { .modes, .grid2 { grid-template-columns:1fr; } body { padding:16px; } }
  .file { display:flex; gap:8px; }
  .file input { flex:1; min-width:0; }
  .value { font-size:40px; font-weight:700; text-align:center; margin:8px 0; font-variant-numeric:tabular-nums; }
  .center { text-align:center; }
  .progress { text-align:center; color:var(--muted); margin-bottom:8px; }
  .status { font-size:13px; text-align:center; min-height:18px; }
  .status.ok { color:var(--ok); } .status.err { color:var(--err); }
  .hint { font-size:13px; color:var(--muted); margin-top:6px; word-break:break-all; }
  .phase { margin:10px 0 4px; padding:10px 12px; border-radius:8px; background:var(--bg); font-size:14px; text-align:center; }
  .bar { height:6px; border-radius:3px; background:var(--border); margin-top:8px; overflow:hidden; }
  .bar > div { height:100%; width:0; background:var(--accent); transition:width .2s; }
  .bar.ok > div { background:var(--ok); }
  ul { list-style:none; padding:0; margin:8px 0 0; max-height:220px; overflow-y:auto; font-family:Consolas, monospace; font-size:14px; border:1px solid var(--border); border-radius:8px; }
  li { padding:6px 10px; border-bottom:1px solid var(--border); }
  li:last-child { border-bottom:none; }
  .hidden { display:none !important; }
  .tip { position:relative; display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; margin-left:6px; border-radius:50%; border:1px solid var(--muted); color:var(--muted); font-size:11px; font-weight:600; cursor:help; vertical-align:middle; user-select:none; }
  .tip:hover, .tip:focus { color:var(--accent); border-color:var(--accent); outline:none; }
  .tipbox { display:none; position:absolute; z-index:20; top:calc(100% + 6px); left:-6px; width:310px; max-width:78vw; padding:10px 12px; background:var(--card); color:var(--text); border:1px solid var(--border); border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.28); font-size:12.5px; font-weight:400; line-height:1.45; text-align:left; white-space:normal; word-break:normal; cursor:default; }
  .tipbox::before { content:""; position:absolute; top:-8px; left:0; right:0; height:8px; }
  .tip:hover .tipbox, .tip:focus .tipbox { display:block; }
  .chartHead { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
  .chartHead strong { font-size:14px; }
  #stats { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; }
  #chart { width:100%; height:280px; display:block; }
</style>
</head>
<body>
<div class="wrap">

  <div class="card" id="setup">
    <h1>Keysight 34461A - Messung</h1>

    <label>Modus<span class="tip" tabindex="0">?<span class="tipbox">Probe-Hold: freihändige DUT-Serie. Ein stabiler Wert wird automatisch erfasst, danach wird auf den Kontaktwechsel zum nächsten DUT gewartet. Messwerte aufzeichnen: Dauermessung im festen Intervall mit Live-Verlauf, auch über Stunden. Einzelmessungen: Der aktuelle Wert wird angezeigt und mit Enter übernommen.</span></span></label>
    <div class="modes">
      <label><input type="radio" name="mode" value="probe" checked><strong>Probe-Hold</strong><span class="d">DUT-Serie freihändig: stabiler Wert wird automatisch erfasst</span></label>
      <label><input type="radio" name="mode" value="trend"><strong>Messwerte aufzeichnen</strong><span class="d">Dauermessung im festen Intervall mit Verlauf</span></label>
      <label><input type="radio" name="mode" value="manual"><strong>Einzelmessungen</strong><span class="d">Wert ansehen und mit Enter übernehmen</span></label>
    </div>

    <h2>Verbindung</h2>
    <div class="grid2">
      <div>
        <label for="ip">IP-Adresse des Geräts<span class="tip" tabindex="0">?<span class="tipbox">Am Gerät unter Utility → I/O Config → LAN Settings ablesen.</span></span></label>
        <input type="text" id="ip" value="169.254.4.61">
      </div>
      <div>
        <label for="conn">Verbindungsart<span class="tip" tabindex="0">?<span class="tipbox">Automatisch: zuerst Raw-Socket (Port 5025), bei Fehlschlag VXI-11. Wie in den bisherigen Skripten.</span></span></label>
        <select id="conn">
          <option value="auto">Automatisch (Socket, sonst VXI-11)</option>
          <option value="socket">nur Socket (Port 5025)</option>
          <option value="vxi11">nur VXI-11</option>
        </select>
      </div>
    </div>

    <h2>Messung am Gerät</h2>
    <div class="grid2">
      <div>
        <label for="func">Messfunktion</label>
        <select id="func"></select>
      </div>
      <div id="rangeBox">
        <label for="range">Messbereich<span class="tip" tabindex="0">?<span class="tipbox">Automatik: Das Gerät wählt den Bereich selbst (etwas langsamer, beim Bereichswechsel kurze Sprünge). Fester Bereich: schneller und ruhiger, wenn die Größenordnung bekannt ist.</span></span></label>
        <select id="range"></select>
      </div>
      <div id="nplcBox">
        <label for="nplc">Integrationszeit (NPLC)<span class="tip" tabindex="0">?<span class="tipbox">Messdauer in Netzperioden (1 PLC = 20 ms bei 50 Hz). Mehr NPLC = ruhigerer Wert, aber langsamer: 0,02 ≈ 0,4 ms, 1 = 20 ms, 10 = 200 ms, 100 = 2 s pro Messung. Ab 1 NPLC wird Netzbrummen unterdrückt.</span></span></label>
        <select id="nplc"></select>
      </div>
      <div>
        <label for="unit">Anzeige- und Speichereinheit<span class="tip" tabindex="0">?<span class="tipbox">In dieser Einheit werden die Werte angezeigt und in der CSV gespeichert. Toleranzen beim Probe-Hold gelten ebenfalls in dieser Einheit.</span></span></label>
        <select id="unit"></select>
      </div>
    </div>
    <label for="extra">Zusätzliche SCPI-Befehle (optional, eine Zeile pro Befehl)<span class="tip" tabindex="0">?<span class="tipbox">Werden nach der Konfiguration ans Gerät geschickt, z.B. SENS:VOLT:DC:ZERO:AUTO OFF oder SENS:VOLT:DC:IMP:AUTO ON. Keine Abfragen (?). Fehler meldet das Gerät beim Verbinden.</span></span></label>
    <textarea id="extra" spellcheck="false"></textarea>

    <div id="box-probe">
      <h2>Probe-Hold</h2>
      <div class="grid2">
        <div>
          <label for="dut_count">Anzahl DUTs</label>
          <input type="number" id="dut_count" min="1" value="10">
        </div>
        <div>
          <label for="beep">Piepton bei Erfassung<span class="tip" tabindex="0">?<span class="tipbox">PC: Ton über die Lautsprecher des PCs (Browser). Gerät: Das Multimeter piept selbst (SYST:BEEP), hörbar direkt am Messplatz. Am Ende der Serie ertönt ein Doppelton.</span></span></label>
          <select id="beep">
            <option value="pc">PC</option>
            <option value="device">Gerät</option>
            <option value="both">PC und Gerät</option>
            <option value="off">aus</option>
          </select>
        </div>
        <div>
          <label for="tol">Stabilitätstoleranz (<span class="u"></span>)<span class="tip" tabindex="0">?<span class="tipbox">So stark dürfen die letzten Werte höchstens schwanken (Max − Min), damit der Wert als stabil gilt und erfasst wird. Gespeichert wird der Mittelwert dieser Werte.</span></span></label>
          <input type="number" id="tol" min="0" step="any" value="1">
        </div>
        <div>
          <label for="stable_n">Anzahl stabile Werte<span class="tip" tabindex="0">?<span class="tipbox">So viele aufeinanderfolgende Werte müssen innerhalb der Toleranz liegen. Zusammen mit dem Abfrageintervall ergibt das die Mindest-Haltezeit der Prüfspitzen.</span></span></label>
          <input type="number" id="stable_n" min="2" value="5">
        </div>
        <div>
          <label for="reset">Wechsel-Schwelle (<span class="u"></span>)<span class="tip" tabindex="0">?<span class="tipbox">Weicht der Wert um mehr als diese Schwelle vom zuletzt erfassten ab, gilt der Kontakt als gelöst bzw. das nächste DUT als angesetzt. Überlast (z.B. offene Spitzen bei Widerstand) zählt immer als Wechsel.</span></span></label>
          <input type="number" id="reset" min="0" step="any" value="50">
        </div>
        <div>
          <label for="idle">Leerlauf-Schwelle (<span class="u"></span>, 0 = aus)<span class="tip" tabindex="0">?<span class="tipbox">Werte, deren Betrag kleiner ist, gelten als "keine Spitze angesetzt" und werden nie erfasst. Verhindert, dass bei abgehobenen Spitzen ein Wert um 0 als DUT gespeichert wird.</span></span></label>
          <input type="number" id="idle" min="0" step="any" value="0">
        </div>
        <div>
          <label for="poll">Abfrageintervall (s)<span class="tip" tabindex="0">?<span class="tipbox">Pause zwischen zwei Abfragen. Die Messdauer selbst (NPLC, Autorange) kommt dazu.</span></span></label>
          <input type="number" id="poll" min="0.05" step="0.05" value="0.15">
        </div>
      </div>
    </div>

    <div id="box-trend" class="hidden">
      <h2>Messwerte aufzeichnen</h2>
      <div class="grid2">
        <div>
          <label for="interval">Messintervall (Sekunden)<span class="tip" tabindex="0">?<span class="tipbox">Zeit zwischen zwei gespeicherten Werten. Dauert eine Messung länger (z.B. NPLC 100 ≈ 2 s), folgt die nächste entsprechend später.</span></span></label>
          <input type="number" id="interval" min="0.1" step="0.1" value="0.5">
        </div>
        <div>
          <label for="trend_count">Anzahl Messungen (0 = unbegrenzt)<span class="tip" tabindex="0">?<span class="tipbox">Bei 0 läuft die Aufzeichnung, bis du auf Stoppen drückst. Jeder Wert wird sofort in die CSV geschrieben, auch wenn der Browser geschlossen wird.</span></span></label>
          <input type="number" id="trend_count" min="0" value="0">
        </div>
      </div>
    </div>

    <div id="box-manual" class="hidden">
      <h2>Einzelmessungen</h2>
      <label for="manual_count">Anzahl Messungen</label>
      <input type="number" id="manual_count" min="1" value="30">
    </div>

    <h2>Datei</h2>
    <label for="path">CSV-Datei<span class="tip" tabindex="0">?<span class="tipbox">Eine vorhandene Datei wird weitergeführt: Neue Werte werden angehängt. Jeder Wert wird sofort gespeichert. Ist die Datei in Excel geöffnet, werden die Werte zwischengespeichert und nachgeschrieben, sobald sie wieder frei ist.</span></span></label>
    <div class="file">
      <input type="text" id="path" value="messungen.csv">
      <button id="btnBrowse">Durchsuchen …</button>
    </div>
    <div class="hint" id="fileInfo"></div>

    <div class="row">
      <button class="primary" id="btnConnect">Verbinden &amp; Gerät einstellen</button>
    </div>
    <div class="status" id="setupStatus" style="text-align:left;margin-top:10px;white-space:pre-line"></div>
  </div>

  <div class="card hidden" id="measure">
    <div class="center" style="color:var(--muted)" id="modeTitle">Aktueller Messwert</div>
    <div class="value" id="value">--</div>
    <div class="progress" id="progress"></div>
    <div class="status ok" id="devInfo"></div>
    <div class="phase hidden" id="phase"><span id="phaseText"></span><div class="bar" id="bar"><div></div></div></div>
    <div class="row" style="justify-content:center">
      <button class="primary hidden" id="btnStart">Start (Enter)</button>
      <button class="primary hidden" id="btnConfirm" title="Übernimmt den aktuell angezeigten Wert und speichert ihn.">Bestätigen (Enter)</button>
      <button class="hidden" id="btnUndo">Letzten überschreiben (Strg+Z)</button>
    </div>
    <div class="status" id="status" style="margin-top:10px"></div>
    <div style="margin-top:12px; color:var(--muted); font-size:13px">Letzte Einträge</div>
    <ul id="history"></ul>
    <div class="hint" id="pathInfo"></div>
    <div class="row" style="justify-content:space-between">
      <button id="btnMenu">← Zurück zum Menü</button>
      <button id="btnQuit">Programm beenden</button>
    </div>
  </div>

  <div class="card hidden" id="chartCard">
    <div class="chartHead">
      <strong>Verlauf der Messwerte</strong>
      <span id="stats"></span>
    </div>
    <canvas id="chart"></canvas>
  </div>

</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const MAX_HISTORY = 200;
const FIELDS = ["ip","conn","func","range","nplc","unit","extra","path","dut_count","beep","tol","stable_n","reset","idle","poll","interval","trend_count","manual_count"];

let META = null;
let S = null;                       // letzter Zustand vom Programm
let rowsFrom = 0, rowsVer = -1;
let nrs = [], vals = [], times = [];
let lastEvent = null;
let polling = false;
let audio = null;
let unitFactor = 1;

async function api(path, body) {
  const r = await fetch(path, { method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify(body || {}) });
  return r.json();
}
function setStatus(el, text, kind) {
  el.textContent = text || "";
  el.className = "status" + (kind ? " " + kind : "");
}
function fmt(v) {
  return Number(v.toPrecision(7)).toLocaleString("de-DE", { useGrouping:false, maximumFractionDigits:12 });
}
function fmtTime(s, step) {
  if (step < 60) return fmt(s) + " s";
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = Math.round(s % 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(r).padStart(2, "0");
}
const mode = () => document.querySelector("input[name=mode]:checked").value;

// ---------- Piepton (PC) ----------
function ensureAudio() {
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch (e) {}
}
function tone(freq, dur, delay) {
  const o = audio.createOscillator(), g = audio.createGain(), t = audio.currentTime + delay;
  o.frequency.value = freq; g.gain.setValueAtTime(0.2, t); g.gain.setValueAtTime(0, t + dur);
  o.connect(g).connect(audio.destination); o.start(t); o.stop(t + dur + 0.02);
}
function beep(kind) {
  if (!audio) return;
  if (kind === "err") tone(600, 0.3, 0);
  else { tone(1500, 0.15, 0); if (kind === "done") tone(1500, 0.15, 0.25); }
}

// ---------- Menue ----------
function fillSelect(sel, items, value) {
  sel.innerHTML = "";
  for (const [v, text] of items) { const o = document.createElement("option"); o.value = v; o.textContent = text; sel.appendChild(o); }
  if (value !== undefined && items.some(i => i[0] === value)) sel.value = value;
}
function onFunctionChange() {
  const f = META.functions[$("func").value];
  fillSelect($("range"), [["AUTO", "Automatik"], ...f.ranges]);
  $("rangeBox").classList.toggle("hidden", !f.ranges.length);
  $("nplcBox").classList.toggle("hidden", !f.nplc);
  fillSelect($("unit"), f.units.map(u => [u[0], u[0]]));
  unitFactor = f.units[0][1];
  updateUnitLabels();
}
function onUnitChange() {
  // Toleranzen in die neue Einheit umrechnen
  const f = META.functions[$("func").value];
  const nf = f.units.find(u => u[0] === $("unit").value)[1];
  for (const id of ["tol", "reset", "idle"]) {
    const v = parseFloat($(id).value);
    if (!isNaN(v)) $(id).value = parseFloat((v * nf / unitFactor).toPrecision(6));
  }
  unitFactor = nf;
  updateUnitLabels();
}
function updateUnitLabels() {
  document.querySelectorAll(".u").forEach(el => el.textContent = $("unit").value);
}
function onModeChange() {
  for (const m of ["probe", "trend", "manual"]) $("box-" + m).classList.toggle("hidden", mode() !== m);
}
function readForm() {
  const cfg = { mode: mode() };
  for (const id of FIELDS) cfg[id === "func" ? "function" : id] = $(id).value;
  return cfg;
}
function applyConfig(c) {
  if (c.mode) { const r = document.querySelector(`input[name=mode][value="${c.mode}"]`); if (r) r.checked = true; }
  if (c.function && META.functions[c.function]) $("func").value = c.function;
  onFunctionChange();
  for (const id of FIELDS) {
    const key = id === "func" ? "function" : id;
    if (id !== "func" && c[key] !== undefined && c[key] !== null) $(id).value = c[key];
  }
  if (!$("range").value) $("range").value = "AUTO";
  if (!$("unit").value) $("unit").selectedIndex = 0;
  const f = META.functions[$("func").value];
  unitFactor = (f.units.find(u => u[0] === $("unit").value) || f.units[0])[1];
  updateUnitLabels();
  onModeChange();
}
function showFileInfo(r) {
  if (!r || !r.ok) return;
  $("fileInfo").textContent = r.path + (r.existing >= 0 ? `  (vorhanden, ${r.existing} Zeilen - wird weitergeführt)` : "  (neue Datei)");
}

document.querySelectorAll("input[name=mode]").forEach(r => r.onchange = onModeChange);
$("func").onchange = onFunctionChange;
$("unit").onchange = onUnitChange;
$("path").onchange = async () => showFileInfo(await api("/api/fileinfo", { path: $("path").value }));
$("btnBrowse").onclick = async () => {
  $("btnBrowse").disabled = true;
  setStatus($("setupStatus"), "Speichern-Dialog ist geöffnet (evtl. hinter dem Browser) …");
  const r = await api("/api/browse", { path: $("path").value });
  $("btnBrowse").disabled = false;
  setStatus($("setupStatus"), r.ok ? "" : r.err, r.ok ? "" : "err");
  if (r.ok && r.path) { $("path").value = r.path; showFileInfo(r); }
};

$("btnConnect").onclick = async () => {
  ensureAudio();
  $("btnConnect").disabled = true;
  setStatus($("setupStatus"), "Verbinde und stelle Gerät ein …");
  let r;
  try { r = await api("/api/connect", readForm()); }
  catch (e) { r = { ok:false, err:"Programm nicht erreichbar - läuft das Python-Fenster noch?" }; }
  $("btnConnect").disabled = false;
  if (!r.ok) { setStatus($("setupStatus"), r.err || "Verbindung fehlgeschlagen.", "err"); return; }
  setStatus($("setupStatus"), "");
  showMeasure();
};

// ---------- Messansicht ----------
function resetRows() {
  rowsFrom = 0; rowsVer = -1; nrs = []; vals = []; times = []; lastEvent = null;
  $("history").innerHTML = "";
}
function showMeasure() {
  resetRows();
  $("setup").classList.add("hidden");
  $("measure").classList.remove("hidden");
  $("chartCard").classList.remove("hidden");
  if (!polling) { polling = true; refresh(); }
}
function showSetup() {
  $("measure").classList.add("hidden");
  $("chartCard").classList.add("hidden");
  $("setup").classList.remove("hidden");
}
function historyText(i) {
  const v = `${fmt(vals[i])} ${S.unit}`;
  if (S.mode === "probe") return `DUT ${nrs[i]}: ${v}`;
  if (S.mode === "trend") return `#${nrs[i]} (${fmtTime(times[i], 1)}): ${v}`;
  return `#${nrs[i]}: ${v}`;
}
function addHistory(i) {
  const ul = $("history"), li = document.createElement("li");
  li.textContent = historyText(i);
  ul.insertBefore(li, ul.firstChild);
  while (ul.children.length > MAX_HISTORY) ul.removeChild(ul.lastChild);
}

async function refresh() {
  if ($("measure").classList.contains("hidden")) { polling = false; return; }
  let s;
  try { s = await (await fetch(`/api/state?since=${rowsFrom}&ver=${rowsVer}`, { cache:"no-store" })).json(); }
  catch (e) {
    setStatus($("status"), "Programm nicht erreichbar - läuft das Python-Fenster noch?", "err");
    setTimeout(refresh, 1000);
    return;
  }
  apply(s);
  setTimeout(refresh, 250);
}

function apply(s) {
  S = s;
  let changed = false;
  if (s.rows_ver !== rowsVer) {
    nrs = []; vals = []; times = []; $("history").innerHTML = ""; rowsVer = s.rows_ver; changed = true;
  }
  for (const r of s.rows) { nrs.push(r[0]); vals.push(r[1]); times.push(r[2]); addHistory(nrs.length - 1); changed = true; }
  rowsFrom = s.rows_from + s.rows.length;

  if (lastEvent === null) lastEvent = s.event_seq;
  else if (s.event_seq > lastEvent) { lastEvent = s.event_seq; if (s.beep_pc || s.event_kind === "err") beep(s.event_kind); }

  $("modeTitle").textContent = { probe:"Probe-Hold · Aktueller Messwert", trend:"Messwerte aufzeichnen · Aktueller Messwert", manual:"Einzelmessungen · Aktueller Messwert" }[s.mode] || "Aktueller Messwert";
  $("value").textContent = !s.connected ? "--" : s.overload ? "Überlast" : s.current === null ? "--" : `${fmt(s.current)} ${s.unit}`;
  $("devInfo").textContent = s.info.length ? "Gerät eingestellt ✓ " + s.info.join(" · ") : "";
  $("pathInfo").textContent = s.path ? "Datei: " + s.path : "";
  setStatus($("status"), s.status, s.status_kind);
  updateProgress(s);
  if (changed) drawChart();
}

function updateProgress(s) {
  const of = s.target > 0 ? ` / ${s.target}` : "";
  const btnStart = $("btnStart"), btnConfirm = $("btnConfirm"), btnUndo = $("btnUndo");
  btnStart.classList.toggle("hidden", s.mode === "manual");
  btnConfirm.classList.toggle("hidden", s.mode !== "manual");
  btnUndo.classList.toggle("hidden", s.mode === "trend");
  $("phase").classList.toggle("hidden", s.mode !== "probe");

  if (s.mode === "manual") {
    $("progress").textContent = s.done ? `Fertig: ${s.count}${of} (Korrektur noch möglich)` : `Messung ${s.count}${of}`;
    btnConfirm.disabled = s.done || !s.connected;
    btnUndo.textContent = "Letzten überschreiben (Strg+Z)";
    btnUndo.disabled = s.count === 0 || !s.connected;
    return;
  }
  btnStart.textContent = s.running ? "Stoppen (Enter)" : (s.count > 0 ? "Fortsetzen (Enter)" : "Start (Enter)");
  btnStart.className = s.running ? "stop" : "primary";
  btnStart.disabled = s.done || !s.connected;

  if (s.mode === "trend") {
    const iv = fmt(s.interval);
    $("progress").textContent = s.done ? `Fertig: ${s.count}${of} Messungen (alle ${iv} s)`
      : (s.running ? `Aufzeichnung läuft: ${s.count}${of} Messungen (alle ${iv} s)` : `Bereit: ${s.count}${of} Messungen (Intervall ${iv} s)`);
    return;
  }
  // Probe-Hold
  btnUndo.textContent = "Letztes DUT neu messen (Strg+Z)";
  btnUndo.disabled = s.count === 0 || !s.connected;
  $("progress").textContent = s.done ? `Fertig: alle ${s.target} DUTs gemessen` : `DUT ${s.count}${of} erfasst`;
  const bar = $("bar");
  let text = "", fill = 0, ok = false;
  if (!s.connected) text = "Keine Verbindung zum Gerät.";
  else if (s.done) { text = "Alle DUTs gemessen ✓"; fill = 1; ok = true; }
  else if (!s.running) text = s.count ? "Pausiert - Fortsetzen (Enter)" : "Bereit - Start (Enter) drücken, dann Prüfspitzen ansetzen.";
  else if (s.phase === "wechsel") { text = `DUT ${s.count} erfasst ✓ - Prüfspitzen lösen und nächstes DUT ansetzen`; fill = 1; ok = true; }
  else {
    fill = s.fill / s.stable_n;
    ok = s.spread !== null && s.spread < s.tol;
    text = `DUT ${s.count + 1}: warte auf stabilen Wert … ${s.fill}/${s.stable_n}` +
      (s.spread !== null ? ` · Streuung ${fmt(s.spread)} ${s.unit} (Toleranz ${fmt(s.tol)})` : "");
  }
  $("phaseText").textContent = text;
  bar.className = "bar" + (ok ? " ok" : "");
  bar.firstElementChild.style.width = Math.round(fill * 100) + "%";
}

$("btnStart").onclick = () => { ensureAudio(); api(S && S.running ? "/api/stop" : "/api/start"); };
$("btnConfirm").onclick = () => api("/api/confirm");
$("btnUndo").onclick = () => api("/api/undo");
$("btnMenu").onclick = async () => {
  if (S && S.running && !confirm("Die Messung läuft noch. Wirklich stoppen und zurück zum Menü?")) return;
  await api("/api/disconnect");
  showSetup();
};
$("btnQuit").onclick = async () => {
  if (!confirm("Programm beenden? Die Verbindung zum Gerät wird getrennt.")) return;
  try { await api("/api/quit"); } catch (e) {}
  document.querySelector(".wrap").innerHTML = '<div class="card center">Programm beendet. Dieses Fenster kann geschlossen werden.</div>';
};
document.addEventListener("keydown", e => {
  if ($("measure").classList.contains("hidden") || !S) return;
  if (e.key === "Enter") {
    e.preventDefault();
    if (S.mode === "manual") $("btnConfirm").click(); else if (!$("btnStart").disabled) $("btnStart").click();
  } else if (e.ctrlKey && e.key.toLowerCase() === "z" && S.mode !== "trend") {
    e.preventDefault();
    if (!$("btnUndo").disabled) $("btnUndo").click();
  }
});

// ---------- Diagramm ----------
function drawChart() {
  const cv = $("chart");
  if (!cv.offsetWidth || !S) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const css = getComputedStyle(document.documentElement);
  const cText = css.getPropertyValue("--text").trim(), cMuted = css.getPropertyValue("--muted").trim();
  const cBorder = css.getPropertyValue("--border").trim(), cAccent = css.getPropertyValue("--accent").trim();
  const cOk = css.getPropertyValue("--ok").trim();
  const trend = S.mode === "trend";
  const n = vals.length;

  const L = 66, R = 14, T = 12, B = 30;
  const pw = W - L - R, ph = H - T - B;
  ctx.font = "12px system-ui, sans-serif";

  if (!n) {
    ctx.fillStyle = cMuted; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(trend ? "Noch keine Messwerte - Aufzeichnung starten" : "Noch keine Messwerte erfasst", W / 2, H / 2);
    $("stats").textContent = "";
    return;
  }

  let vmin = Infinity, vmax = -Infinity, sum = 0;
  for (const v of vals) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; sum += v; }
  const mean = sum / n;
  let min = vmin, max = vmax;
  if (min === max) { const d = Math.abs(min) * 0.01 || 1; min -= d; max += d; }
  else { const d = (max - min) * 0.12; min -= d; max += d; }

  // X-Werte: Probe-Hold/Einzelmessung = Nummer, Aufzeichnung = Sekunden
  const xs = trend ? times : nrs.map(k => k - 1);
  const xmin = 0;
  let xmax;
  if (trend) {
    const last = xs[n - 1];
    xmax = S.target > 0 ? Math.max((S.target - 1) * S.interval, last, 1) : Math.max(last, 10);
  } else {
    xmax = Math.max(S.target, nrs[n - 1], 2) - 1;
  }
  const X = x => L + ((x - xmin) / (xmax - xmin)) * pw;
  const Y = v => T + (1 - (v - min) / (max - min)) * ph;

  ctx.lineWidth = 1; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let k = 0; k < 5; k++) {
    const v = min + (max - min) * k / 4, y = Y(v);
    ctx.strokeStyle = cBorder; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
    ctx.fillStyle = cMuted; ctx.fillText(fmt(Number(v.toPrecision(4))), L - 8, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  if (trend) {
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400, 172800];
    const maxTicks = Math.max(2, Math.floor(pw / 60));
    const st = steps.find(s => xmax / s <= maxTicks) || steps[steps.length - 1];
    for (let t = 0; t <= xmax + 1e-9; t += st) {
      const x = X(t);
      ctx.strokeStyle = cBorder; ctx.beginPath(); ctx.moveTo(x, T + ph); ctx.lineTo(x, T + ph + 4); ctx.stroke();
      ctx.fillStyle = cMuted; ctx.fillText(fmtTime(t, st), x, H - B + 8);
    }
  } else {
    const cnt = xmax + 1;
    const step = Math.max(1, Math.ceil(cnt / Math.floor(pw / 32)));
    for (let i = 0; i < cnt; i++) {
      if (i !== 0 && i !== cnt - 1 && i % step !== 0) continue;
      ctx.fillStyle = cMuted; ctx.fillText(String(i + 1), X(i), H - B + 8);
    }
  }
  ctx.save(); ctx.translate(12, T + ph / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillStyle = cMuted; ctx.textAlign = "center"; ctx.textBaseline = "top";
  ctx.fillText(S.unit, 0, -4); ctx.restore();

  // Mittelwert
  ctx.setLineDash([5, 4]); ctx.strokeStyle = cOk; ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.moveTo(L, Y(mean)); ctx.lineTo(W - R, Y(mean)); ctx.stroke();
  ctx.setLineDash([]);

  // Linie; bei sehr vielen Werten pro Bildpunkt-Spalte nur Min/Max zeichnen
  ctx.strokeStyle = cAccent; ctx.lineWidth = n > 300 ? 1.5 : 2; ctx.lineJoin = "round";
  ctx.beginPath();
  if (n > pw * 2) {
    let col = null, lo = 0, hi = 0;
    const flush = () => { ctx.lineTo(col, Y(lo)); ctx.lineTo(col, Y(hi)); };
    for (let i = 0; i < n; i++) {
      const c = Math.round(X(xs[i]));
      if (c !== col) { if (col !== null) flush(); else ctx.moveTo(c, Y(vals[i])); col = c; lo = hi = vals[i]; }
      else { if (vals[i] < lo) lo = vals[i]; if (vals[i] > hi) hi = vals[i]; }
    }
    flush();
  } else {
    vals.forEach((v, i) => i ? ctx.lineTo(X(xs[i]), Y(v)) : ctx.moveTo(X(xs[i]), Y(v)));
  }
  ctx.stroke();
  if (n <= 60) {
    vals.forEach((v, i) => { ctx.beginPath(); ctx.arc(X(xs[i]), Y(v), 3.5, 0, Math.PI * 2); ctx.fillStyle = cAccent; ctx.fill(); });
  }
  ctx.beginPath(); ctx.arc(X(xs[n - 1]), Y(vals[n - 1]), 6, 0, Math.PI * 2);
  ctx.strokeStyle = cText; ctx.lineWidth = 1.5; ctx.stroke();

  let sq = 0;
  for (const v of vals) sq += (v - mean) ** 2;
  const sd = n > 1 ? Math.sqrt(sq / (n - 1)) : 0;
  $("stats").textContent = `n ${n} · Ø ${fmt(mean)} ${S.unit} · Min ${fmt(vmin)} · Max ${fmt(vmax)} · σ ${fmt(sd)}`;
}
window.addEventListener("resize", drawChart);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", drawChart);

// ---------- Start ----------
(async () => {
  META = await (await fetch("/api/meta", { cache:"no-store" })).json();
  fillSelect($("func"), Object.entries(META.functions).map(([k, f]) => [k, f.label]));
  fillSelect($("nplc"), [["", "Standard (10)"], ...META.nplc.map(v => [v, v.replace(".", ",")])]);
  applyConfig(META.config || {});
  if ($("path").value) showFileInfo(await api("/api/fileinfo", { path: $("path").value }));
  // Laeuft noch eine Messung (Seite neu geladen)? Dann direkt die Messansicht zeigen
  const s = await (await fetch("/api/state?since=0&ver=-1", { cache:"no-store" })).json();
  if (s.connected || s.lost) showMeasure();
})();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    main()
