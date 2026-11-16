#!/usr/bin/env python3
"""TDK-Lambda Z+ 20V/20A (USB/COM, SCPI): Stromprofil fahren und loggen, bedient im Browser.

Installation:  pip install pyvisa pyvisa-py pyserial
Start:         python z_plus_web.py   (oder Doppelklick auf die Datei)
               Der Browser öffnet sich mit der Bedienoberfläche: http://127.0.0.1:8765
Beenden:       Im Browser "Stopp", danach im Fenster des Skripts Strg+C.
               Der Ausgang wird beim Beenden immer abgeschaltet.

- Die Oberfläche ist nur auf diesem PC erreichbar, nicht im Netzwerk.
- Ein laufendes Profil läuft weiter, wenn der Browser geschlossen wird.
  Einfach die Adresse wieder öffnen, der Status wird wieder angezeigt.
- Das Fenster des Skripts muss während des Laufs offen bleiben.
- "Simulation" testet die Oberfläche ohne Gerät (Last 1.5 Ohm wird nachgebildet).
- Das Log (CSV) liegt im selben Ordner wie dieses Skript.
"""
import csv
import ctypes
import json
import os
import random
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import pyvisa
    from pyvisa.errors import VisaIOError
except ImportError:                      # Simulation funktioniert auch ohne pyvisa
    pyvisa = None

    class VisaIOError(Exception):
        pass

# ======================================================================
#  Vorgaben (lassen sich alle in der Oberfläche ändern)
# ======================================================================
PORT = 8765                 # Adresse der Oberfläche: http://127.0.0.1:8765
GERAET_V_MAX = 20.0         # Nennspannung des Geräts
GERAET_I_MAX = 20.0         # Nennstrom des Geräts
TERMINATOR = "\r"           # Zeilenende, hat an diesem Gerät funktioniert
PRUEF_ALLE_S = 10.0         # so oft wird geprüft, ob der Ausgang noch an ist
KONSOLE_ALLE_S = 60.0       # Abstand der Statuszeilen im Fenster des Skripts

VORGABEN = {
    "com": 5, "baud": 57600, "adr": 6, "sim": False,
    "vgrenze": 20.0, "imax": 20.0,
    "int_lang": 1.0, "int_kurz": 0.2, "kurz_grenze": 600.0,
    "komma": True, "standby": True, "wdh": 2,
    "profile": {
        "Testprofil (10 s)": [(0.0, 1.0), (5.0, 1.5), (10.0, 1.5)],
        "Echtes Profil": [
            (0.0, 2.39), (15067.90, 2.39), (15067.91, 3.05), (17995.80, 3.05),
            (17995.81, 3.50), (43494.30, 3.50), (43494.31, 4.16), (45224.00, 4.16),
            (45224.01, 4.71), (45382.10, 4.71), (45382.11, 5.48), (45870.10, 5.48),
            (45870.11, 6.32), (45873.10, 6.32),
        ],
    },
}
VORGABEN["profil"] = "Echtes Profil"

LOG_ORDNER = os.path.dirname(os.path.abspath(__file__))


class Fehler(Exception):
    """Fehler mit verständlicher Meldung für die Oberfläche."""


# ======================================================================
#  Hilfsfunktionen (wie in z_plus_mvp.py)
# ======================================================================

def standby_verhindern(an):
    """Windows: kein Standby durch Leerlauf, solange der Lauf-Thread lebt.
    Verhindert NICHT: Deckel zuklappen, Herunterfahren, Neustart, Abmelden."""
    if sys.platform != "win32":
        return
    funktion = ctypes.windll.kernel32.SetThreadExecutionState
    funktion.argtypes = [ctypes.c_uint]
    funktion(0x80000000 | (0x00000001 if an else 0))   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def pruefe_profil(profil, strom_max):
    if len(profil) < 2:
        raise Fehler("Profil braucht mindestens 2 Zeilen (Start bei 0 s und Ende).")
    if profil[0][0] != 0:
        raise Fehler(f"Profil: Zeile 1 muss bei 0 s beginnen (steht: {profil[0][0]} s).")
    for n, ((t1, _), (t2, _)) in enumerate(zip(profil, profil[1:]), start=2):
        if t2 <= t1:
            raise Fehler(f"Profil: Zeile {n} ({t2} s) liegt nicht nach Zeile {n - 1} ({t1} s). "
                         "Zeiten müssen streng ansteigen.")
    for n, (t, i) in enumerate(profil, start=1):
        if not 0 <= i <= strom_max:
            raise Fehler(f"Profil: Zeile {n} ({t} s): Strom {i} A ausserhalb 0..{strom_max} A.")


def aenderungen(profil):
    """Nur die Zeilen, in denen sich der Sollwert ändert."""
    stufen = [profil[0]]
    for t, i in profil[1:]:
        if i != stufen[-1][1]:
            stufen.append((t, i))
    return stufen


def intervalle_je_stufe(stufen, t_ende, int_kurz, int_lang, kurz_grenze):
    intervalle = []
    for k, (t_start, _) in enumerate(stufen):
        t_stufenende = stufen[k + 1][0] if k + 1 < len(stufen) else t_ende
        intervalle.append(int_kurz if t_stufenende - t_start <= kurz_grenze else int_lang)
    return intervalle


class SimPSU:
    """Nachbildung des Netzgeräts mit ohmscher Last, zum Testen ohne Gerät."""
    LAST_OHM = 1.5

    def __init__(self):
        self.volt = self.curr = 0.0
        self.an = False

    def write(self, befehl):
        time.sleep(0.005)
        teile = befehl.strip().upper().split()
        if teile[0] == "VOLT":
            self.volt = float(teile[1])
        elif teile[0] == "CURR":
            self.curr = float(teile[1])
        elif teile[0] == "OUTP:STAT":
            self.an = teile[1] in ("ON", "1")

    def query(self, befehl):
        time.sleep(0.03)
        befehl = befehl.strip().upper()
        if befehl == "*IDN?":
            return "SIMULATION,Z+20-20 (kein Gerät),0,1.0"
        if befehl == "SYST:ERR?":
            return '0,"No error"'
        if befehl == "OUTP:STAT?":
            return "ON" if self.an else "OFF"
        i = min(self.curr, self.volt / self.LAST_OHM) if self.an else 0.0
        if befehl == "MEAS:CURR?":
            return f"{max(0.0, i + random.uniform(-0.004, 0.004)):.3f}"
        if befehl == "MEAS:VOLT?":
            return f"{max(0.0, i * self.LAST_OHM + random.uniform(-0.004, 0.004)):.3f}"
        return "0"

    def close(self):
        pass


# ======================================================================
#  Steuerung: Verbindung, Lauf im Hintergrund, Status für die Oberfläche
# ======================================================================

class Steuerung:
    MAX_PUNKTE = 20000      # danach wird jeder 2. Punkt fürs Diagramm verworfen (CSV bleibt komplett)

    def __init__(self):
        self.lock = threading.RLock()
        self.psu = None
        self.rm = None
        self.thread = None
        self.stop = threading.Event()
        self.lauf_id = 0
        self.seq = 0
        self.punkte = []
        self.meldungen = []
        self.z = {
            "verbunden": False, "idn": "", "simulation": False, "phase": "getrennt",
            "durchlauf": 0, "wiederholungen": 0, "dauer": 0.0, "profil": [],
            "t": 0.0, "t_gesamt": 0.0, "soll": None, "v": None, "i": None,
            "stufe": 0, "stufen": 0, "messen": True, "logdatei": "", "startzeit": None,
            "vgrenze": 0.0,
        }

    # --- Hilfen ---------------------------------------------------------
    def meldung(self, text):
        zeit = datetime.now().strftime("%H:%M:%S")
        print(f"{zeit}  {text}")
        with self.lock:
            self.seq += 1
            self.meldungen.append((self.seq, zeit, text))
            del self.meldungen[:-300]

    def laeuft(self):
        return self.thread is not None and self.thread.is_alive()

    def setze(self, **werte):
        with self.lock:
            self.z.update(werte)

    def _sende(self, befehl, pause=0.02):
        self.psu.write(befehl)
        time.sleep(pause)

    def _fehler_pruefen(self):
        try:
            antwort = self.psu.query("SYST:ERR?").strip()
        except VisaIOError:
            return
        if not antwort.startswith("0"):
            self.meldung(f"Geräte-Fehler: {antwort}")

    def _ausgang_aus(self):
        try:
            antwort = self.psu.query("OUTP:STAT?").strip().upper()
        except VisaIOError:
            return False
        return antwort in ("0", "OFF")

    # --- Verbindung -----------------------------------------------------
    def verbinden(self, d):
        if self.laeuft():
            raise Fehler("Während eines Laufs nicht möglich.")
        self.trennen(still=True)
        if d.get("sim"):
            psu, idn = SimPSU(), None
        else:
            if pyvisa is None:
                raise Fehler("pyvisa ist nicht installiert: pip install pyvisa pyvisa-py pyserial")
            com = int(zahl(d, "com", 1, 256, "COM-Port"))
            baud = int(zahl(d, "baud", 300, 1000000, "Baudrate"))
            adr = int(zahl(d, "adr", 1, 31, "Adresse"))
            ressource = f"ASRL{com}::INSTR"
            if self.rm is None:
                try:
                    self.rm = pyvisa.ResourceManager()
                except (OSError, ValueError):
                    self.rm = pyvisa.ResourceManager("@py")
            try:
                psu = self.rm.open_resource(ressource, open_timeout=5000)
            except (VisaIOError, OSError, ValueError) as e:
                raise Fehler(
                    f"COM{com} lässt sich nicht öffnen ({e}). Meist belegt ein anderes Programm "
                    "den Port (z_plus_mvp.py, altes python.exe im Task-Manager, PuTTY) oder die "
                    "COM-Nummer hat sich geändert (Geräte-Manager prüfen).")
            psu.timeout = 2000
            psu.baud_rate = baud
            psu.write_termination = TERMINATOR
            psu.read_termination = TERMINATOR
            try:
                psu.write(f"INST:NSEL {adr}")
                time.sleep(0.02)
                idn = psu.query("*IDN?").strip()
            except (VisaIOError, OSError) as e:
                psu.close()
                raise Fehler(f"Das Gerät an COM{com} antwortet nicht ({e}). Baudrate und Adresse "
                             "prüfen, am Gerät: REM -> INtF = USB, LanG = SCPI.")
        if idn is None:
            idn = psu.query("*IDN?").strip()
        self.psu = psu
        self.setze(verbunden=True, idn=idn, simulation=bool(d.get("sim")), phase="bereit")
        self.meldung(f"Verbunden: {idn}")
        return {"idn": idn}

    def trennen(self, d=None, still=False):
        if self.laeuft():
            raise Fehler("Erst den Lauf stoppen.")
        if self.psu is not None:
            try:
                self.psu.close()
            except Exception:
                pass
            self.psu = None
            if not still:
                self.meldung("Verbindung getrennt.")
        self.setze(verbunden=False, idn="", phase="getrennt")

    # --- Lauf -------------------------------------------------------------
    def start(self, d):
        if self.psu is None:
            raise Fehler("Zuerst mit dem Gerät verbinden.")
        if self.laeuft():
            raise Fehler("Es läuft bereits ein Profil.")
        p = {
            "vgrenze": zahl(d, "vgrenze", 0.1, GERAET_V_MAX, "Spannungsgrenze"),
            "imax": zahl(d, "imax", 0.1, GERAET_I_MAX, "Strom max."),
            "int_lang": zahl(d, "int_lang", 0.1, 3600, "Messintervall lange Stufen"),
            "int_kurz": zahl(d, "int_kurz", 0.1, 3600, "Messintervall kurze Stufen"),
            "kurz_grenze": zahl(d, "kurz_grenze", 0, 1e7, "Grenze kurze Stufe"),
            "wdh": int(zahl(d, "wdh", 0, 100000, "Wiederholungen")),
            "komma": bool(d.get("komma", True)),
            "standby": bool(d.get("standby", True)),
        }
        try:
            p["profil"] = [(float(t), float(i)) for t, i in d["profil"]]
        except (KeyError, TypeError, ValueError):
            raise Fehler("Profil ist ungültig.")
        pruefe_profil(p["profil"], p["imax"])
        p["logdatei"] = os.path.join(LOG_ORDNER, f"z_plus_log_{datetime.now():%Y%m%d_%H%M%S}.csv")

        self.stop.clear()
        with self.lock:
            self.lauf_id += 1
            self.punkte = []
            self.z.update(phase="laeuft", durchlauf=0, wiederholungen=p["wdh"],
                          dauer=p["profil"][-1][0], profil=p["profil"], t=0.0, t_gesamt=0.0,
                          soll=p["profil"][0][1], v=None, i=None, stufe=0, stufen=0, messen=True,
                          logdatei=p["logdatei"], startzeit=time.time(), vgrenze=p["vgrenze"])
        self.thread = threading.Thread(target=self._lauf, args=(p,), daemon=True)
        self.thread.start()
        return {}

    def stoppen(self, d=None):
        if self.laeuft():
            self.meldung("Stopp angefordert.")
            self.stop.set()
        return {}

    def _lauf(self, p):
        if p["standby"]:
            standby_verhindern(True)
        ende = "beendet"
        anzahl = p["wdh"] if p["wdh"] else "endlos"
        try:
            self._sende("*CLS", pause=0.3)               # *CLS braucht ca. 150 ms
            self._sende("SYST:ERR:ENAB")
            self._sende(f"VOLT {p['vgrenze']}")
            self._sende(f"CURR {p['profil'][0][1]}")
            self._sende("OUTP:STAT ON")
            self._fehler_pruefen()
            self.meldung(f"Ausgang eingeschaltet. Log: {p['logdatei']}")
            with open(p["logdatei"], "w", newline="", encoding="utf-8") as f:
                log = csv.writer(f, delimiter=";")
                log.writerow(["durchlauf", "zeit_s", "zeit_gesamt_s", "soll_A", "mess_V", "mess_A"])
                tg0 = time.perf_counter()
                n = 1
                while p["wdh"] == 0 or n <= p["wdh"]:
                    self.setze(durchlauf=n)
                    self.meldung(f"Durchlauf {n} von {anzahl} gestartet.")
                    if n > 1:
                        self._sende(f"CURR {p['profil'][0][1]}")   # zurück zum Startwert
                    ende = self._durchlauf(p, f, log, n, tg0)
                    if ende != "beendet":
                        break
                    n += 1
        except Exception as e:
            ende = "fehler"
            self.meldung(f"Fehler, Lauf beendet: {e}")
        finally:
            try:
                self._sende("OUTP:STAT OFF")
                self.meldung("Ausgang ausgeschaltet.")
                self._fehler_pruefen()
            except Exception as e:
                self.meldung(f"ACHTUNG: Ausgang konnte nicht ausgeschaltet werden ({e}). "
                             "Bitte am Gerät abschalten!")
            if p["standby"]:
                standby_verhindern(False)
            texte = {"beendet": "Alle Durchläufe beendet.", "abgebrochen": "Lauf abgebrochen.",
                     "fehler": "Lauf wegen Fehler beendet."}
            self.meldung(texte[ende])
            self.setze(phase=ende)

    def _durchlauf(self, p, f, log, n, tg0):
        """Ein Durchlauf des Profils. Zeit t beginnt bei jedem Durchlauf neu bei 0."""
        profil = p["profil"]
        stufen = aenderungen(profil)
        t_ende = profil[-1][0]
        intervalle = intervalle_je_stufe(stufen, t_ende, p["int_kurz"], p["int_lang"],
                                         p["kurz_grenze"])
        if p["komma"]:
            def zahl_txt(x, nachkomma):
                return f"{x:.{nachkomma}f}".replace(".", ",")
        else:
            def zahl_txt(x, nachkomma):
                return f"{x:.{nachkomma}f}"
        soll = stufen[0][1]
        intervall = intervalle[0]
        k = 1
        naechste_messung = 0.0
        naechste_pruefung = PRUEF_ALLE_S
        naechste_ausgabe = 0.0
        messfehler = 0
        cv_gemeldet = False
        self.setze(soll=soll, stufe=1, stufen=len(stufen))
        t0 = time.perf_counter()

        while (t := time.perf_counter() - t0) < t_ende:
            if self.stop.is_set():
                return "abgebrochen"
            tg = time.perf_counter() - tg0

            if k < len(stufen) and t >= stufen[k][0]:
                soll = stufen[k][1]                       # Sollwert hat Vorrang
                self._sende(f"CURR {soll}")
                self.meldung(f"Durchlauf {n}, t = {t:.1f} s: Sollwert {soll} A")
                intervall = intervalle[k]
                naechste_messung = t                      # sofort in dieser Stufe messen
                k += 1
                self.setze(soll=soll, stufe=k)

            elif self.z["messen"] and t >= naechste_messung:
                try:
                    v = float(self.psu.query("MEAS:VOLT?"))
                    i = float(self.psu.query("MEAS:CURR?"))
                    messfehler = 0
                except (VisaIOError, ValueError):
                    v = i = None
                    messfehler += 1
                    if messfehler >= 3:
                        self.setze(messen=False)
                        self.meldung("Messung abgeschaltet (3 Fehler in Folge), Profil läuft weiter.")
                log.writerow([n, zahl_txt(t, 2), zahl_txt(tg, 2), zahl_txt(soll, 3),
                              "" if v is None else zahl_txt(v, 4),
                              "" if i is None else zahl_txt(i, 4)])
                f.flush()
                # nach einer Verzögerung (z. B. Timeout) nicht im Schnelldurchlauf nachholen
                naechste_messung = max(naechste_messung + intervall, t)
                if v is not None:
                    with self.lock:
                        self.z.update(v=v, i=i)
                        self.seq += 1
                        self.punkte.append((self.seq, round(tg, 2), soll, v, i))
                        if len(self.punkte) > self.MAX_PUNKTE:
                            self.punkte = self.punkte[::2]
                    if t >= naechste_ausgabe:
                        print(f"   Durchlauf {n}  t={t:9.1f} s  soll={soll} A  mess={v} V / {i} A")
                        naechste_ausgabe = t + KONSOLE_ALLE_S
                    if v >= 0.98 * p["vgrenze"] and i < soll - 0.1 and not cv_gemeldet:
                        cv_gemeldet = True
                        self.meldung("Hinweis: Gerät regelt auf die Spannungsgrenze, der Strom liegt "
                                     "unter dem Sollwert. Keine Last, Last zu hochohmig oder "
                                     "Spannungsgrenze zu niedrig.")

            elif t >= naechste_pruefung:
                naechste_pruefung = t + PRUEF_ALLE_S
                if self._ausgang_aus():
                    self.meldung("Das Gerät hat den Ausgang selbst abgeschaltet (Schutzabschaltung "
                                 "wie OVP/OTP, Taste am Gerät oder Netzausfall).")
                    self._fehler_pruefen()
                    return "fehler"
            else:
                time.sleep(0.005)
            self.setze(t=t, t_gesamt=tg)
        return "beendet"

    # --- Status und Beenden ---------------------------------------------
    def status(self, lauf_id, seit_punkt, seit_meldung):
        with self.lock:
            if lauf_id != self.lauf_id:
                seit_punkt = 0
            punkte = [pt for pt in self.punkte if pt[0] > seit_punkt][:5000]
            meldungen = [m for m in self.meldungen if m[0] > seit_meldung]
            return {"z": dict(self.z, laeuft=self.laeuft()), "lauf_id": self.lauf_id,
                    "punkte": punkte, "meldungen": meldungen}

    def beenden(self, warten_s):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(warten_s)
        if not self.laeuft():
            self.trennen(still=True)


def zahl(d, schluessel, unten, oben, name):
    try:
        x = float(d[schluessel])
    except (KeyError, TypeError, ValueError):
        raise Fehler(f"{name}: keine gültige Zahl.")
    if not unten <= x <= oben:
        raise Fehler(f"{name} muss zwischen {unten:g} und {oben:g} liegen.")
    return x


# ======================================================================
#  Webserver
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    steuerung = None

    def log_message(self, *args):
        pass

    def _antwort(self, code, inhalt, typ):
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(inhalt)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(inhalt)

    def _json(self, daten, code=200):
        self._antwort(code, json.dumps(daten).encode("utf-8"), "application/json; charset=utf-8")

    def _erlaubt(self):
        # Schutz davor, dass fremde Webseiten im Browser das Netzgerät steuern
        host = self.headers.get("Host", "")
        return (host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
                and self.headers.get("X-Z-Plus") == "1")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._antwort(200, SEITE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/status" and self._erlaubt():
            q = parse_qs(url.query)
            def wert(name):
                try:
                    return int(q.get(name, ["0"])[0])
                except ValueError:
                    return 0
            self._json(self.steuerung.status(wert("lauf"), wert("p"), wert("m")))
        else:
            self._json({"ok": False, "fehler": "Nicht gefunden"}, 404)

    def do_POST(self):
        aktionen = {"/api/verbinden": self.steuerung.verbinden,
                    "/api/trennen": self.steuerung.trennen,
                    "/api/start": self.steuerung.start,
                    "/api/stopp": self.steuerung.stoppen}
        aktion = aktionen.get(urlparse(self.path).path)
        if aktion is None or not self._erlaubt():
            self._json({"ok": False, "fehler": "Nicht erlaubt"}, 403)
            return
        try:
            laenge = int(self.headers.get("Content-Length", 0))
            daten = json.loads(self.rfile.read(laenge) or b"{}")
            ergebnis = aktion(daten) or {}
            self._json({"ok": True, **ergebnis})
        except Fehler as e:
            self._json({"ok": False, "fehler": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._json({"ok": False, "fehler": f"Unerwarteter Fehler: {e}"})


def fenster_schliessen_abfangen(funktion):
    """Windows: Wird das Fenster des Skripts geschlossen, zuerst den Ausgang abschalten."""
    if sys.platform != "win32":
        return None
    typ = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def handler(ereignis):
        if ereignis in (2, 5, 6):        # Fenster schliessen, Abmelden, Herunterfahren
            funktion()
            return 1
        return 0                          # Strg+C normal behandeln

    rueckruf = typ(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(rueckruf, True)
    return rueckruf                       # Referenz behalten, sonst räumt Python sie weg


def main():
    adresse = f"http://127.0.0.1:{PORT}"
    steuerung = Steuerung()
    Handler.steuerung = steuerung
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} ist belegt, das Skript läuft wahrscheinlich schon. Öffne {adresse}")
        webbrowser.open(adresse)
        return
    server.daemon_threads = True
    _rueckruf = fenster_schliessen_abfangen(lambda: steuerung.beenden(4))
    print(f"Bedienoberfläche: {adresse}")
    print("Dieses Fenster während des Laufs offen lassen. Beenden mit Strg+C.")
    threading.Timer(0.5, webbrowser.open, args=(adresse,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Beende ...")
    finally:
        steuerung.beenden(10)
        server.server_close()
        print("Ausgang aus, Verbindung geschlossen.")


# ======================================================================
#  Bedienoberfläche (HTML, wird vom Skript ausgeliefert)
# ======================================================================

SEITE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Z+ Stromprofil</title>
<style>
  :root {
    --bg: #f4f5f7; --karte: #fff; --text: #1d2330; --leise: #667085; --linie: #dde1e7;
    --blau: #2563eb; --gruen: #16a34a; --gruen-d: #15803d; --rot: #dc2626; --rot-d: #b91c1c;
    --orange: #d97706; --grau: #9aa3b2; --feld: #fff; --hinweis: #fff7e6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12151b; --karte: #1b1f27; --text: #e6e9ef; --leise: #98a2b3; --linie: #2c323d;
      --blau: #60a5fa; --gruen: #22c55e; --gruen-d: #16a34a; --rot: #ef4444; --rot-d: #dc2626;
      --orange: #f59e0b; --grau: #5b6474; --feld: #12151b; --hinweis: #2a2414;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px;
           padding: 14px 20px; background: var(--karte); border-bottom: 1px solid var(--linie);
           position: sticky; top: 0; z-index: 2; flex-wrap: wrap; }
  h1 { font-size: 19px; margin: 0; }
  h2 { font-size: 15px; margin: 0 0 12px; text-transform: uppercase; letter-spacing: .04em;
       color: var(--leise); }
  .badge { padding: 4px 12px; border-radius: 999px; font-weight: 600; font-size: 13px;
           background: var(--linie); color: var(--leise); }
  .badge.bereit { background: #dbeafe; color: #1e40af; }
  .badge.laeuft { background: #dcfce7; color: #166534; }
  .badge.fehler { background: #fee2e2; color: #991b1b; }
  main { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 16px;
         padding: 16px 20px; max-width: 1500px; margin: 0 auto; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; padding: 12px; } }
  .spalte { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .karte { background: var(--karte); border: 1px solid var(--linie); border-radius: 10px;
           padding: 16px; }
  .reihe { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .reihe2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
  label { display: block; font-size: 13px; color: var(--leise); margin-bottom: 3px; }
  input[type=number], select, textarea {
    width: 100%; padding: 7px 9px; border: 1px solid var(--linie); border-radius: 6px;
    background: var(--feld); color: var(--text); font: inherit; }
  textarea { font-family: ui-monospace, Consolas, monospace; font-size: 13px; resize: vertical;
             min-height: 180px; }
  input:disabled, select:disabled, textarea:disabled { opacity: .6; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--text);
           font-size: 14px; }
  button { font: inherit; font-weight: 600; border: 0; border-radius: 7px; padding: 8px 14px;
           cursor: pointer; background: var(--linie); color: var(--text); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primaer { background: var(--blau); color: #fff; }
  .knoepfe { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; align-items: center; }
  .gross { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .gross button { padding: 16px; font-size: 18px; border-radius: 10px; color: #fff; }
  #start { background: var(--gruen); } #start:not(:disabled):hover { background: var(--gruen-d); }
  #stopp { background: var(--rot); } #stopp:not(:disabled):hover { background: var(--rot-d); }
  .klein { font-size: 13px; color: var(--leise); }
  .meldung-ok { color: var(--gruen); } .meldung-fehler { color: var(--rot); white-space: pre-line; }
  details summary { cursor: pointer; font-weight: 600; color: var(--leise); }
  details[open] summary { margin-bottom: 10px; }
  .werte { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  @media (max-width: 600px) { .werte { grid-template-columns: repeat(2, 1fr); } }
  .wert { background: var(--bg); border-radius: 8px; padding: 10px 12px; }
  .wert .titel { font-size: 12px; color: var(--leise); text-transform: uppercase; }
  .wert .zahl { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .wert .zahl small { font-size: 15px; font-weight: 600; color: var(--leise); }
  .info { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 6px 16px;
          margin-top: 12px; font-size: 14px; }
  .info b { font-variant-numeric: tabular-nums; }
  .balken { height: 10px; background: var(--linie); border-radius: 5px; overflow: hidden;
            margin-top: 12px; }
  .balken div { height: 100%; width: 0; background: var(--gruen); transition: width .5s; }
  canvas { width: 100%; height: 340px; display: block; }
  .legende { display: flex; gap: 16px; font-size: 13px; color: var(--leise); flex-wrap: wrap; }
  .legende span::before { content: ""; display: inline-block; width: 14px; height: 3px;
                          margin-right: 6px; vertical-align: middle; background: var(--c); }
  #meldungen { max-height: 260px; overflow-y: auto; font-size: 13px;
               font-family: ui-monospace, Consolas, monospace; }
  #meldungen div { padding: 3px 0; border-bottom: 1px solid var(--linie); }
  #meldungen .zeit { color: var(--leise); margin-right: 8px; }
  #offline { display: none; background: var(--rot); color: #fff; padding: 10px 20px;
             font-weight: 600; text-align: center; }
  .warnung { background: var(--hinweis); border-radius: 8px; padding: 8px 10px; font-size: 13px;
             margin-top: 10px; }
  code { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
</style>
</head>
<body>
<div id="offline">Keine Verbindung zum Skript. Läuft <code>z_plus_web.py</code> noch?</div>
<header>
  <h1>TDK-Lambda Z+ &middot; Stromprofil</h1>
  <span id="badge" class="badge">Nicht verbunden</span>
</header>
<main>
  <div class="spalte">
    <section class="karte">
      <h2>1 &middot; Verbindung</h2>
      <div class="reihe">
        <div><label for="com">COM-Port</label><input id="com" type="number" min="1" step="1"></div>
        <div><label for="baud">Baudrate</label>
          <select id="baud">
            <option>9600</option><option>19200</option><option>38400</option>
            <option>57600</option><option>115200</option>
          </select></div>
        <div><label for="adr">Adresse</label><input id="adr" type="number" min="1" max="31" step="1"></div>
      </div>
      <label class="check"><input id="sim" type="checkbox"> Simulation (ohne Gerät testen)</label>
      <div class="knoepfe">
        <button id="verbinden" class="primaer">Verbinden</button>
        <button id="trennen">Trennen</button>
      </div>
      <div id="verb_info" class="klein" style="margin-top:8px">
        Am Gerät: REM &rarr; INtF = USB, LanG = SCPI. COM-Nummer steht im Geräte-Manager.
      </div>
    </section>

    <section class="karte">
      <h2>2 &middot; Profil</h2>
      <div class="knoepfe" id="vorlagen" style="margin-top:0;margin-bottom:10px"></div>
      <label for="profil">Eine Zeile pro Punkt: <b>Zeit [s] ; Strom [A]</b>. Der Strom gilt ab
        dieser Zeit, die letzte Zeit ist das Ende. Zwei Spalten aus Excel lassen sich direkt einfügen.</label>
      <textarea id="profil" spellcheck="false"></textarea>
      <div id="profil_info" class="klein" style="margin-top:6px"></div>
      <div class="reihe2" style="margin-top:12px">
        <div><label for="wdh">Durchläufe (0 = endlos)</label>
          <input id="wdh" type="number" min="0" step="1"></div>
        <div><label>Gesamtdauer</label><div id="gesamtdauer" style="padding-top:6px;font-weight:600">&ndash;</div></div>
      </div>
    </section>

    <section class="karte">
      <details>
        <summary>Grenzen und Messung</summary>
        <div class="reihe2">
          <div><label for="vgrenze">Spannungsgrenze [V]</label>
            <input id="vgrenze" type="number" min="0" max="20" step="0.1"></div>
          <div><label for="imax">Strom max. [A]</label>
            <input id="imax" type="number" min="0" max="20" step="0.1"></div>
        </div>
        <div class="klein" style="margin-top:6px">Strom fliesst höchstens bis Spannungsgrenze / Lastwiderstand.
          Nur so hoch wählen wie nötig, ohne Last liegt sie an den Klemmen.</div>
        <div class="reihe" style="margin-top:12px">
          <div><label for="int_lang">Intervall lang [s]</label>
            <input id="int_lang" type="number" min="0.1" step="0.1"></div>
          <div><label for="int_kurz">Intervall kurz [s]</label>
            <input id="int_kurz" type="number" min="0.1" step="0.1"></div>
          <div><label for="kurz_grenze">Kurz bis [s]</label>
            <input id="kurz_grenze" type="number" min="0" step="1"></div>
        </div>
        <div class="klein" style="margin-top:6px">Stufen bis "Kurz bis" werden mit dem kurzen Intervall
          gemessen, längere mit dem langen. Untergrenze ca. 0.2 s.</div>
        <label class="check"><input id="komma" type="checkbox"> Dezimalkomma im Log (für deutsches Excel)</label>
        <label class="check"><input id="standby" type="checkbox"> Windows-Standby während des Laufs verhindern</label>
        <div class="knoepfe"><button id="zuruecksetzen">Vorgaben wiederherstellen</button></div>
      </details>
    </section>

    <section class="gross">
      <button id="start" disabled>&#9654; Start</button>
      <button id="stopp" disabled>&#9632; Stopp</button>
    </section>
    <div id="aktion_info" class="klein"></div>
  </div>

  <div class="spalte">
    <section class="karte">
      <h2>Status</h2>
      <div class="werte">
        <div class="wert"><div class="titel">Soll</div><div class="zahl" id="w_soll">&ndash;</div></div>
        <div class="wert"><div class="titel">Strom gemessen</div><div class="zahl" id="w_i">&ndash;</div></div>
        <div class="wert"><div class="titel">Spannung gemessen</div><div class="zahl" id="w_v">&ndash;</div></div>
        <div class="wert"><div class="titel">Durchlauf</div><div class="zahl" id="w_dl">&ndash;</div></div>
      </div>
      <div class="balken"><div id="fortschritt"></div></div>
      <div class="info">
        <div>Zeit im Durchlauf: <b id="i_t">&ndash;</b></div>
        <div>Stufe: <b id="i_stufe">&ndash;</b></div>
        <div>Verstrichen gesamt: <b id="i_tg">&ndash;</b></div>
        <div>Restzeit: <b id="i_rest">&ndash;</b></div>
        <div>Ende ca.: <b id="i_ende">&ndash;</b></div>
      </div>
      <div id="i_log" class="klein" style="margin-top:8px;word-break:break-all"></div>
      <div id="i_warn"></div>
    </section>

    <section class="karte">
      <h2 id="diagramm_titel">Profil-Vorschau</h2>
      <div class="legende">
        <span style="--c: var(--grau)">Profil (Soll)</span>
        <span style="--c: var(--blau)">Strom gemessen [A]</span>
        <span style="--c: var(--orange)">Spannung gemessen [V, rechte Achse]</span>
      </div>
      <canvas id="diagramm"></canvas>
    </section>

    <section class="karte">
      <h2>Meldungen</h2>
      <div id="meldungen"></div>
    </section>
  </div>
</main>

<script>
const VORGABEN = __VORGABEN__;
const $ = id => document.getElementById(id);
const FELDER = ["com", "baud", "adr", "sim", "vgrenze", "imax", "int_lang", "int_kurz",
                "kurz_grenze", "komma", "standby", "wdh", "profil"];
const SPEICHER = "z_plus_web_einstellungen";
const PHASEN = {getrennt: "Nicht verbunden", bereit: "Verbunden, bereit", laeuft: "Läuft",
                beendet: "Fertig", abgebrochen: "Abgebrochen", fehler: "Fehler"};

let vorschau = false, zustand = null, laufId = -1, seitP = 0, seitM = 0, punkte = [], online = true, beschaeftigt = false;

// ---------- Einstellungen ----------
function profilText(profil) { return profil.map(([t, i]) => `${t} ; ${i}`).join("\n"); }

function vorgabenSetzen() {
  for (const f of FELDER) {
    if (f === "profil") $(f).value = profilText(VORGABEN.profile[VORGABEN.profil]);
    else if (typeof VORGABEN[f] === "boolean") $(f).checked = VORGABEN[f];
    else $(f).value = VORGABEN[f];
  }
}
function laden() {
  vorgabenSetzen();
  try {
    const d = JSON.parse(localStorage.getItem(SPEICHER) || "{}");
    for (const f of FELDER) if (f in d) {
      if ($(f).type === "checkbox") $(f).checked = d[f]; else $(f).value = d[f];
    }
  } catch (e) {}
}
function speichern() {
  const d = {};
  for (const f of FELDER) d[f] = $(f).type === "checkbox" ? $(f).checked : $(f).value;
  try { localStorage.setItem(SPEICHER, JSON.stringify(d)); } catch (e) {}
}

// ---------- Profil lesen und prüfen ----------
function zahlLesen(s) {
  s = s.trim();
  if (s.includes(",") && !s.includes(".")) s = s.replace(",", ".");
  if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return NaN;
  return parseFloat(s);
}
function profilLesen(text) {
  const profil = [];
  const zeilen = text.split(/\r?\n/);
  for (let n = 0; n < zeilen.length; n++) {
    let z = zeilen[n].replace(/[()\[\]]/g, " ").trim().replace(/,$/, "").trim();
    if (!z || z.startsWith("#")) continue;
    let teile;
    if (/[;\t]/.test(z)) teile = z.split(/\s*[;\t]\s*/);
    else if (/\s/.test(z)) teile = z.split(/[\s,]+/).filter(Boolean);
    else if ((z.match(/,/g) || []).length === 1) teile = z.split(",");
    else teile = [z];
    teile = teile.map(s => s.replace(/,$/, "")).filter(s => s !== "");
    if (teile.length !== 2) {
      if (profil.length === 0 && /[a-zA-Z]/.test(z)) continue;   // Kopfzeile überspringen
      return {fehler: `Zeile ${n + 1}: zwei Werte erwartet (Zeit ; Strom), gefunden: "${zeilen[n].trim()}"`};
    }
    const t = zahlLesen(teile[0]), i = zahlLesen(teile[1]);
    if (isNaN(t) || isNaN(i)) {
      if (profil.length === 0 && /[a-zA-Z]/.test(z)) continue;
      return {fehler: `Zeile ${n + 1}: keine gültige Zahl in "${zeilen[n].trim()}"`};
    }
    profil.push([t, i]);
  }
  const imax = parseFloat($("imax").value);
  if (profil.length < 2) return {fehler: "Mindestens 2 Zeilen nötig (Start bei 0 s und Ende)."};
  if (profil[0][0] !== 0) return {fehler: `Die erste Zeit muss 0 sein (steht: ${profil[0][0]} s).`};
  for (let k = 1; k < profil.length; k++)
    if (profil[k][0] <= profil[k - 1][0])
      return {fehler: `Punkt ${k + 1} (${profil[k][0]} s) liegt nicht nach Punkt ${k} (${profil[k - 1][0]} s).`};
  for (let k = 0; k < profil.length; k++)
    if (profil[k][1] < 0 || profil[k][1] > imax)
      return {fehler: `Punkt ${k + 1} (${profil[k][0]} s): Strom ${profil[k][1]} A ausserhalb 0..${imax} A.`};
  return {profil};
}
function stufenZahl(profil) {
  let n = 1;
  for (let k = 1; k < profil.length; k++) if (profil[k][1] !== profil[k - 1][1]) n++;
  return n;
}

// ---------- Formatierung ----------
function dauer(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sek = s % 60;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(sek).padStart(2, "0")} s`;
  return `${sek} s`;
}
function uhrzeit(ms) {
  const d = new Date(ms), heute = new Date();
  const zeit = d.toLocaleTimeString("de-DE", {hour: "2-digit", minute: "2-digit"});
  if (d.toDateString() === heute.toDateString()) return `heute ${zeit}`;
  return d.toLocaleDateString("de-DE", {weekday: "short", day: "2-digit", month: "2-digit"}) + " " + zeit;
}
function zahlTxt(x, nk, einheit) {
  return x == null ? "–" : `${x.toFixed(nk)} <small>${einheit}</small>`;
}

// ---------- Profil-Anzeige links ----------
function profilAnzeigen() {
  const r = profilLesen($("profil").value);
  const wdh = parseInt($("wdh").value, 10);
  if (r.fehler) {
    $("profil_info").innerHTML = `<span class="meldung-fehler">&#9888; ${r.fehler}</span>`;
    $("gesamtdauer").textContent = "–";
  } else {
    const p = r.profil, d = p[p.length - 1][0];
    const max = Math.max(...p.map(x => x[1]));
    $("profil_info").innerHTML = `<span class="meldung-ok">&#10003; ${p.length} Punkte, ` +
      `${stufenZahl(p)} Stufen, ${dauer(d)} pro Durchlauf, max. ${max} A</span>`;
    $("gesamtdauer").textContent = wdh > 0 ? dauer(d * wdh) : "endlos";
  }
  return r;
}

// ---------- Diagramm ----------
function farbe(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function zeichnen() {
  const c = $("diagramm"), dpr = window.devicePixelRatio || 1;
  const B = c.clientWidth, H = c.clientHeight;
  if (c.width !== Math.round(B * dpr) || c.height !== Math.round(H * dpr)) {
    c.width = Math.round(B * dpr); c.height = Math.round(H * dpr);
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, B, H);

  const imLauf = zustand && zustand.profil && zustand.profil.length && laufId > 0 &&
                 (zustand.laeuft || !vorschau);
  let profil, wdh, vgrenze;
  if (imLauf) { profil = zustand.profil; wdh = zustand.wiederholungen; vgrenze = zustand.vgrenze; }
  else {
    const r = profilLesen($("profil").value);
    profil = r.profil || []; wdh = parseInt($("wdh").value, 10) || 0;
    vgrenze = parseFloat($("vgrenze").value) || 20;
  }
  $("diagramm_titel").textContent = imLauf ? "Verlauf" : "Profil-Vorschau";
  if (!profil.length) return;
  const d = profil[profil.length - 1][0];
  const pkt = imLauf ? punkte : [];
  const tLetzt = pkt.length ? pkt[pkt.length - 1][1] : 0;
  const n = wdh > 0 ? wdh : Math.max(1, Math.ceil((tLetzt + 1e-9) / d) || 1);
  const xMax = Math.max(d * n, tLetzt);
  let iMax = Math.max(...profil.map(p => p[1]));
  for (const p of pkt) if (p[4] > iMax) iMax = p[4];
  const [iTop, iStep] = schoeneAchse(iMax);
  iMax = iTop;
  let vMax = 0;
  for (const p of pkt) if (p[3] > vMax) vMax = p[3];
  const [vTop, vStep] = schoeneAchse(Math.max(vMax, 1));
  vMax = vTop;

  const L = 46, R = 46, O = 10, U = 30, pb = B - L - R, ph = H - O - U;
  const x = t => L + t / xMax * pb, yI = i => O + ph - i / iMax * ph, yV = v => O + ph - v / vMax * ph;
  const einheit = xMax > 7200 ? [3600, "h"] : xMax > 180 ? [60, "min"] : [1, "s"];

  g.font = "12px system-ui, sans-serif"; g.lineWidth = 1;
  g.strokeStyle = farbe("--linie"); g.fillStyle = farbe("--leise");
  g.textAlign = "right"; g.textBaseline = "middle";
  const nTicks = Math.round(iMax / iStep);
  for (let k = 0; k <= nTicks; k++) {
    const yy = yI(k * iStep);
    g.beginPath(); g.moveTo(L, yy); g.lineTo(L + pb, yy); g.stroke();
    g.fillText(+(k * iStep).toFixed(2), L - 6, yy);
  }
  if (pkt.length) {
    g.textAlign = "left";
    for (let v = 0; v <= vMax + 1e-9; v += vStep) g.fillText(+v.toFixed(2), L + pb + 6, yV(v));
    g.textAlign = "right";
  }
  g.textAlign = "center"; g.textBaseline = "top";
  const schritt = schoeneSchritte(xMax / einheit[0]);
  for (let v = 0; v <= xMax / einheit[0] + 1e-9; v += schritt)
    g.fillText(+(v.toFixed(3)) + " " + einheit[1], x(v * einheit[0]), O + ph + 8);
  g.save(); g.translate(12, O + ph / 2); g.rotate(-Math.PI / 2); g.textBaseline = "middle";
  g.fillText("A", 0, 0); g.restore();

  // Profil (Soll), über alle Durchläufe
  g.strokeStyle = farbe("--grau"); g.lineWidth = 2; g.beginPath();
  for (let r = 0; r < n; r++) {
    const o = r * d;
    for (let k = 0; k < profil.length; k++) {
      const [t, i] = profil[k];
      if (r === 0 && k === 0) g.moveTo(x(o + t), yI(i));
      else { if (k > 0) g.lineTo(x(o + t), yI(profil[k - 1][1])); g.lineTo(x(o + t), yI(i)); }
    }
  }
  g.stroke();
  if (!pkt.length) return;
  const linie = (idx, y, f) => {
    g.strokeStyle = farbe(f); g.lineWidth = 1.5; g.beginPath();
    pkt.forEach((p, k) => k ? g.lineTo(x(p[1]), y(p[idx])) : g.moveTo(x(p[1]), y(p[idx])));
    g.stroke();
  };
  linie(3, yV, "--orange");
  linie(4, yI, "--blau");
}
function schoeneAchse(max) {
  const step = schoeneSchritte(max * 1.1 * 6 / 5);
  return [Math.ceil(max * 1.1 / step) * step, step];
}
function schoeneSchritte(max) {
  const roh = max / 6, p = Math.pow(10, Math.floor(Math.log10(roh || 1)));
  for (const f of [1, 2, 2.5, 5, 10]) if (roh <= f * p) return f * p;
  return 10 * p;
}

// ---------- Server ----------
async function api(pfad, daten) {
  const opt = {headers: {"X-Z-Plus": "1", "Content-Type": "application/json"}};
  if (daten !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(daten); }
  const r = await fetch(pfad, opt);
  return r.json();
}

async function aktion(pfad, daten) {
  beschaeftigt = true; knoepfe();
  $("aktion_info").innerHTML = "";
  try {
    const r = await api(pfad, daten || {});
    if (!r.ok) $("aktion_info").innerHTML = `<span class="meldung-fehler">&#9888; ${r.fehler}</span>`;
    return r;
  } catch (e) {
    $("aktion_info").innerHTML = `<span class="meldung-fehler">&#9888; Keine Verbindung zum Skript.</span>`;
    return {ok: false};
  } finally { beschaeftigt = false; await abfragen(); }
}

async function abfragen() {
  let r;
  try { r = await api(`/api/status?lauf=${laufId}&p=${seitP}&m=${seitM}`); }
  catch (e) { online = false; $("offline").style.display = "block"; knoepfe(); return; }
  online = true; $("offline").style.display = "none";
  if (r.lauf_id !== laufId) { punkte = []; seitP = 0; laufId = r.lauf_id; }
  for (const p of r.punkte) punkte.push(p);
  if (r.punkte.length) seitP = r.punkte[r.punkte.length - 1][0];
  if (punkte.length > 20000) punkte = punkte.filter((_, k) => k % 2 === 0);
  if (r.meldungen.length) {
    const box = $("meldungen");
    for (const [seq, zeit, text] of r.meldungen) {
      const div = document.createElement("div");
      div.innerHTML = `<span class="zeit">${zeit}</span>`;
      div.appendChild(document.createTextNode(text));
      if (/ACHTUNG|Fehler|abgeschaltet/.test(text)) div.style.color = "var(--rot)";
      box.prepend(div);
      seitM = seq;
    }
    while (box.children.length > 300) box.lastChild.remove();
  }
  zustand = r.z;
  anzeigen();
}

// ---------- Anzeige ----------
function anzeigen() {
  const z = zustand;
  if (!z) return;
  const badge = $("badge");
  badge.textContent = PHASEN[z.phase] || z.phase;
  badge.className = "badge " + ({laeuft: "laeuft", fehler: "fehler", getrennt: ""}[z.phase] ?? "bereit");
  if (z.verbunden) {
    $("verb_info").innerHTML = `Verbunden mit: <b></b>`;
    $("verb_info").querySelector("b").textContent = z.idn;
  } else {
    $("verb_info").textContent = "Am Gerät: REM → INtF = USB, LanG = SCPI. COM-Nummer steht im Geräte-Manager.";
  }
  const gelaufen = laufId > 0 && z.profil.length;
  $("w_soll").innerHTML = gelaufen && z.laeuft ? zahlTxt(z.soll, 2, "A") : "–";
  $("w_i").innerHTML = gelaufen ? zahlTxt(z.i, 3, "A") : "–";
  $("w_v").innerHTML = gelaufen ? zahlTxt(z.v, 3, "V") : "–";
  $("w_dl").innerHTML = gelaufen ? `${z.durchlauf} <small>/ ${z.wiederholungen || "&infin;"}</small>` : "–";
  if (gelaufen) {
    $("i_t").textContent = `${dauer(z.t)} von ${dauer(z.dauer)}`;
    $("i_stufe").textContent = `${z.stufe} von ${z.stufen}`;
    $("i_tg").textContent = dauer(z.t_gesamt);
    if (z.wiederholungen > 0) {
      const gesamt = z.dauer * z.wiederholungen, rest = Math.max(0, gesamt - z.t_gesamt);
      $("i_rest").textContent = z.laeuft ? dauer(rest) : "–";
      $("i_ende").textContent = z.laeuft ? uhrzeit(Date.now() + rest * 1000) : "–";
      $("fortschritt").style.width = (z.phase === "beendet" ? 100 : Math.min(100, z.t_gesamt / gesamt * 100)) + "%";
    } else {
      $("i_rest").textContent = "endlos"; $("i_ende").textContent = "Stopp drücken";
      $("fortschritt").style.width = Math.min(100, z.t / z.dauer * 100) + "%";
    }
    $("i_log").innerHTML = `Log: <code></code>`; $("i_log").querySelector("code").textContent = z.logdatei;
    $("i_warn").innerHTML = z.laeuft && !z.messen
      ? `<div class="warnung">Messung ausgefallen, das Profil läuft weiter.</div>` : "";
  }
  knoepfe();
  zeichnen();
}

function knoepfe() {
  const z = zustand || {}, laeuft = !!z.laeuft, verbunden = !!z.verbunden;
  const ok = !profilLesen($("profil").value).fehler;
  $("verbinden").disabled = !online || beschaeftigt || laeuft;
  $("verbinden").textContent = verbunden ? "Neu verbinden" : "Verbinden";
  $("trennen").disabled = !online || beschaeftigt || laeuft || !verbunden;
  $("start").disabled = !online || beschaeftigt || laeuft || !verbunden || !ok;
  $("stopp").disabled = !online || !laeuft;
  for (const f of FELDER) $(f).disabled = laeuft;
  for (const b of document.querySelectorAll("#vorlagen button, #zuruecksetzen")) b.disabled = laeuft;
  let hinweis = "";
  if (!laeuft && !beschaeftigt && online) {
    if (!verbunden) hinweis = "Zum Starten zuerst verbinden.";
    else if (!ok) hinweis = "Profil korrigieren, dann starten.";
  }
  if (hinweis && !$("aktion_info").querySelector(".meldung-fehler")) $("aktion_info").textContent = hinweis;
  else if (!hinweis && !$("aktion_info").querySelector(".meldung-fehler")) $("aktion_info").textContent = "";
}

// ---------- Bedienung ----------
function einstellungen() {
  const d = {};
  for (const f of FELDER) d[f] = $(f).type === "checkbox" ? $(f).checked : $(f).value;
  return d;
}

$("verbinden").onclick = () => aktion("/api/verbinden", einstellungen());
$("trennen").onclick = () => aktion("/api/trennen");
$("stopp").onclick = () => aktion("/api/stopp");
$("start").onclick = async () => {
  const r = profilLesen($("profil").value);
  if (r.fehler) return;
  const d = einstellungen(), p = r.profil, wdh = parseInt(d.wdh, 10);
  const dd = p[p.length - 1][0];
  const text = "Ausgang einschalten und Profil starten?\n\n" +
    `Max. Strom: ${Math.max(...p.map(x => x[1]))} A\n` +
    `Spannungsgrenze: ${d.vgrenze} V\n` +
    `Durchläufe: ${wdh > 0 ? wdh : "endlos"}\n` +
    `Dauer: ${wdh > 0 ? dauer(dd * wdh) : "bis Stopp"}` +
    (zustand && zustand.simulation ? "\n\n(SIMULATION, kein Gerät)" : "");
  if (!confirm(text)) return;
  d.profil = p;
  vorschau = false;
  await aktion("/api/start", d);
};
$("zuruecksetzen").onclick = () => {
  if (!confirm("Alle Felder auf die Vorgaben zurücksetzen?")) return;
  vorgabenSetzen(); speichern(); profilAnzeigen(); knoepfe(); zeichnen();
};
for (const name of Object.keys(VORGABEN.profile)) {
  const b = document.createElement("button");
  b.textContent = name;
  b.onclick = () => {
    $("profil").value = profilText(VORGABEN.profile[name]);
    vorschau = true;
    speichern(); profilAnzeigen(); knoepfe(); zeichnen();
  };
  $("vorlagen").appendChild(b);
}
for (const f of FELDER) $(f).addEventListener("input", () => {
  if (!(zustand && zustand.laeuft)) vorschau = true;
  speichern(); profilAnzeigen(); knoepfe(); if (!(zustand && zustand.laeuft)) zeichnen();
});
window.addEventListener("resize", zeichnen);
window.addEventListener("beforeunload", e => {
  if (zustand && zustand.laeuft) { e.preventDefault(); e.returnValue = ""; }
});

laden();
profilAnzeigen();
zeichnen();
abfragen();
setInterval(abfragen, 1000);
</script>
</body>
</html>
"""
SEITE = SEITE.replace("__VORGABEN__", json.dumps(VORGABEN))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("Fehler. Enter zum Schliessen ...")
