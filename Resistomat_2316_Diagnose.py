"""
RESISTOMAT 2316 - Diagnose der RS232-Verbindung
-----------------------------------------------
Findet heraus, woran die Kommunikation mit einem RESISTOMAT scheitert,
wenn Resistomat_2316_Messung.py bei einem Geraet funktioniert, bei einem
anderen aber nicht.

Ablauf:
1. Skript starten (Doppelklick oder "python Resistomat_2316_Diagnose.py"),
   COM-Port waehlen und einen Namen fuer das Geraet eingeben (z.B. "Geraet 2").
2. Das Skript fuehrt nacheinander diese Tests durch und protokolliert
   jedes gesendete und empfangene Byte:
     1) COM-Port / USB-Adapter (Treiber, VID:PID, Seriennummer)
     2) Port oeffnen + Steuerleitungen (CTS, DSR, CD, RI)
     3) Standardtest mit exakt den Einstellungen aus Resistomat_2316_Messung.py
     4) Varianten: Handshake (DTR/RTS, RTS/CTS), langsames Senden, BLOCKCHECK
     5) Adress-Scan (falls das Geraet eine andere Adresse als 0000 hat)
     6) Scan von Baudrate und Datenformat (8N1, 7E1, ...)
3. Am Ende steht eine Auswertung mit der wahrscheinlichen Ursache.
   Bericht wird als .txt (zum Lesen) und .json (zum Vergleichen) gespeichert.

Empfehlung: Das Skript an ALLEN DREI Geraeten laufen lassen, auch an dem,
das funktioniert. Danach die Berichte vergleichen:
    python Resistomat_2316_Diagnose.py --vergleich bericht1.json bericht2.json bericht3.json

Weitere Optionen:
    --port COM3         COM-Port direkt angeben
    --name "Geraet 2"   Name fuer den Bericht
    --voll              kombinierter Scan Adresse x Baudrate x Format (dauert lange)
    --loopback          Kabel/Adapter-Test: am Kabelende (Geraeteseite) Pin 2 und 3
                        des DSUB9-Steckers brücken, Geraet abstecken

Voraussetzung:
- pip install pyserial
- Das Messskript darf waehrenddessen NICHT laufen (COM-Port waere belegt).
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

import serial
import serial.tools.list_ports

# Einstellungen wie in Resistomat_2316_Messung.py
SCRIPT_BAUDRATE = 9600
SCRIPT_FORMAT = "8N1"
SCRIPT_ADDR = "0000"

STX = b"\x02"
ETX = b"\x03"
EOT = b"\x04"
ENQ = b"\x05"
ACK = b"\x06"
LF = b"\x0A"
NAK = b"\x15"

SCAN_TIMEOUT = 0.3       # s, Wartezeit auf eine Antwort beim Scannen
DETAIL_TIMEOUT = 2.0     # s, Wartezeit beim Standardtest

SCAN_BAUDS = [9600, 19200, 4800, 2400, 1200, 38400, 57600, 115200]
SCAN_FORMATS = {
    "8N1": (serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE),
    "7E1": (serial.SEVENBITS, serial.PARITY_EVEN, serial.STOPBITS_ONE),
    "7O1": (serial.SEVENBITS, serial.PARITY_ODD, serial.STOPBITS_ONE),
    "8E1": (serial.EIGHTBITS, serial.PARITY_EVEN, serial.STOPBITS_ONE),
    "8O1": (serial.EIGHTBITS, serial.PARITY_ODD, serial.STOPBITS_ONE),
    "8N2": (serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_TWO),
}
# Geraeteadressen 0..31, sowohl vierstellig ("0003") als auch doppelt
# geschrieben ("0303"), da beide Schreibweisen bei DIN-66019-Geraeten vorkommen.
SCAN_ADDRS = list(dict.fromkeys(
    [f"{a:04d}" for a in range(32)] + [f"{a:02d}{a:02d}" for a in range(32)]
))

# USB-Seriell-Chips, die oefter Probleme machen (Clones, alte Treiber)
KNOWN_ADAPTERS = {
    "067B": "Prolific PL2303 - Clones laufen mit neuen Windows-Treibern oft nicht (Geraetemanager pruefen, ggf. aelteren Treiber)",
    "1A86": "WCH CH340/CH341 - guenstiger Chip, meist ok, Treiber pruefen",
    "0403": "FTDI - in der Regel zuverlaessig",
    "10C4": "Silicon Labs CP210x - in der Regel zuverlaessig",
}

CTRL_NAMES = {0x02: "<STX>", 0x03: "<ETX>", 0x04: "<EOT>", 0x05: "<ENQ>",
              0x06: "<ACK>", 0x0A: "<LF>", 0x0D: "<CR>", 0x15: "<NAK>", 0x00: "<NUL>"}


# ---------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------
class Report:
    def __init__(self):
        self.lines = []

    def log(self, text=""):
        print(text)
        self.lines.append(text)

    def section(self, title):
        self.log()
        self.log("=" * 70)
        self.log(title)
        self.log("=" * 70)


def readable(data: bytes) -> str:
    out = []
    for b in data:
        if b in CTRL_NAMES:
            out.append(CTRL_NAMES[b])
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"<{b:02X}>")
    return "".join(out) if out else "(nichts)"


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data) if data else "-"


def block_check(data: bytes) -> bytes:
    """BCC nach DIN 66019: XOR ueber alle Zeichen nach STX bis inkl. ETX."""
    value = 0
    for b in data:
        value ^= b
    return bytes([value])


def build_select(addr: str, command: str, with_bcc=False) -> bytes:
    body = command.encode("ascii") + LF + ETX
    frame = EOT + addr.encode("ascii") + b"sr" + STX + body
    if with_bcc:
        frame += block_check(body)
    return frame


def open_port(port, baud=SCRIPT_BAUDRATE, fmt=SCRIPT_FORMAT, timeout=DETAIL_TIMEOUT,
              rtscts=False, dtr=True, rts=True):
    bytesize, parity, stopbits = SCAN_FORMATS[fmt]
    ser = serial.serial_for_url(port, do_not_open=True)
    ser.baudrate = baud
    ser.bytesize = bytesize
    ser.parity = parity
    ser.stopbits = stopbits
    ser.timeout = timeout
    ser.rtscts = rtscts
    ser.dtr = dtr
    ser.rts = rts
    ser.open()
    time.sleep(0.05)
    ser.reset_input_buffer()
    return ser


def write(ser, data: bytes, slow=False):
    if slow:
        for b in data:
            ser.write(bytes([b]))
            ser.flush()
            time.sleep(0.005)
    else:
        ser.write(data)
        ser.flush()


def read_reply(ser) -> bytes:
    """Liest das erste Byte (mit Timeout) und sammelt danach alles ein,
    was noch kurz hinterherkommt - so sieht man auch 'Muell'-Bytes."""
    data = ser.read(1)
    if not data:
        return b""
    deadline = time.time() + 0.15
    while time.time() < deadline:
        waiting = ser.in_waiting
        if waiting:
            data += ser.read(waiting)
            deadline = time.time() + 0.05
        else:
            time.sleep(0.01)
    return data


def classify(tx: bytes, rx: bytes) -> str:
    if not rx:
        return "keine Antwort"
    if rx[:1] == ACK:
        return "ACK"
    if rx[:1] == NAK:
        return "NAK"
    if len(rx) >= 3 and (tx.startswith(rx) or rx.startswith(tx)):
        return "Echo"
    return "unlesbar"


def transact(ser, addr, command, with_bcc=False, slow=False, report=None) -> dict:
    """Ein kompletter Austausch wie im Messskript: Select (sr) -> ACK,
    dann Poll (po) -> Antworttelegramm."""
    result = {"befehl": command, "adresse": addr, "status": None,
              "tx_select": "", "rx_select": "", "tx_poll": "", "rx_poll": "", "antwort": None}
    ser.reset_input_buffer()

    frame = build_select(addr, command, with_bcc)
    write(ser, frame, slow)
    rx = read_reply(ser)
    result["tx_select"] = readable(frame)
    result["rx_select"] = readable(rx)
    result["status"] = classify(frame, rx)
    if report:
        report.log(f"  TX Select : {readable(frame)}")
        report.log(f"              [{hexs(frame)}]")
        report.log(f"  RX        : {readable(rx)}   [{hexs(rx)}]  -> {result['status']}")

    if result["status"] != "ACK":
        return result

    poll_frame = EOT + addr.encode("ascii") + b"po" + ENQ
    write(ser, poll_frame, slow)
    raw = ser.read_until(ETX)
    if with_bcc and raw.endswith(ETX):
        raw += ser.read(1)
    result["tx_poll"] = readable(poll_frame)
    result["rx_poll"] = readable(raw)
    if report:
        report.log(f"  TX Poll   : {readable(poll_frame)}")
        report.log(f"  RX        : {readable(raw)}")

    if STX in raw:
        ser.write(ACK)
        ser.read(1)  # abschliessendes EOT
        answer = raw.split(STX, 1)[1].split(ETX, 1)[0].decode("ascii", errors="replace").strip()
        result["antwort"] = answer
        result["status"] = "OK"
        if report:
            report.log(f"  Antwort   : '{answer}'")
    else:
        result["status"] = "ACK, aber keine Antwort auf Poll"
    return result


def quick_probe(port, baud, fmt, addr, with_bcc=False, rtscts=False, dtr=True, rts=True, slow=False):
    """Kurzer Test mit *idn?, gibt Ergebnis-Dict oder Fehlertext zurueck."""
    try:
        ser = open_port(port, baud, fmt, SCAN_TIMEOUT, rtscts=rtscts, dtr=dtr, rts=rts)
    except Exception as e:  # z.B. Einstellung vom Adapter nicht unterstuetzt
        return {"status": "Port-Fehler", "fehler": str(e)}
    try:
        return transact(ser, addr, "*idn?", with_bcc=with_bcc, slow=slow)
    except Exception as e:
        return {"status": "Port-Fehler", "fehler": str(e)}
    finally:
        ser.close()


# ---------------------------------------------------------
# Einzelne Tests
# ---------------------------------------------------------
def test_port_info(port, report, data):
    report.section("1) COM-Port / USB-Adapter")
    info = None
    for p in serial.tools.list_ports.comports():
        if p.device.upper() == port.upper():
            info = p
    if info is None:
        report.log(f"  {port} wurde in der Port-Liste nicht gefunden.")
        data["port_info"] = None
        return
    vid = f"{info.vid:04X}" if info.vid is not None else None
    pid = f"{info.pid:04X}" if info.pid is not None else None
    data["port_info"] = {
        "device": info.device, "beschreibung": info.description, "hersteller": info.manufacturer,
        "hwid": info.hwid, "vid": vid, "pid": pid, "seriennummer": info.serial_number,
    }
    report.log(f"  Port         : {info.device}")
    report.log(f"  Beschreibung : {info.description}")
    report.log(f"  Hersteller   : {info.manufacturer}")
    report.log(f"  HWID         : {info.hwid}")
    if vid:
        report.log(f"  VID:PID      : {vid}:{pid}")
        if vid in KNOWN_ADAPTERS:
            report.log(f"  Hinweis      : {KNOWN_ADAPTERS[vid]}")
    else:
        report.log("  Kein USB-Adapter (eingebaute/onboard Schnittstelle oder virtueller Port)")


def test_open_and_lines(port, report, data):
    report.section("2) Port oeffnen + Steuerleitungen")
    try:
        ser = open_port(port)
    except serial.SerialException as e:
        report.log(f"  FEHLER: Port laesst sich nicht oeffnen: {e}")
        report.log("  -> Port belegt (Messskript/anderes Programm offen?) oder falscher Port.")
        data["port_offen"] = False
        data["port_fehler"] = str(e)
        return False
    data["port_offen"] = True
    lines = {}
    try:
        for name in ("cts", "dsr", "cd", "ri"):
            try:
                lines[name.upper()] = bool(getattr(ser, name))
            except (serial.SerialException, NotImplementedError, AttributeError, OSError):
                lines[name.upper()] = None
    finally:
        ser.close()
    data["steuerleitungen"] = lines
    report.log("  Port geoeffnet: OK")
    report.log("  Steuerleitungen (DTR/RTS vom PC auf EIN gesetzt):")
    for k, v in lines.items():
        report.log(f"    {k}: {'EIN' if v else ('AUS' if v is False else 'unbekannt')}")
    if lines.get("DSR") or lines.get("CTS"):
        report.log("  -> Gegenstelle legt Steuerleitungen an: Kabel ist am Geraet angeschlossen.")
    else:
        report.log("  -> Keine Steuerleitung aktiv. Bei 3-Draht-Kabel normal, sonst Hinweis auf")
        report.log("     fehlendes Kabel/ausgeschaltetes Geraet. Mit funktionierendem Geraet vergleichen!")
    return True


def test_loopback(port, report, data):
    report.section("Loopback-Test (Pin 2-3 gebrueckt, Geraet abgesteckt)")
    try:
        ser = open_port(port, timeout=1.0)
    except serial.SerialException as e:
        report.log(f"  FEHLER: {e}")
        data["loopback"] = "Port-Fehler"
        return
    try:
        probe = b"LOOPBACK-TEST-1234567890"
        write(ser, probe)
        rx = ser.read(len(probe))
    finally:
        ser.close()
    ok = rx == probe
    data["loopback"] = "OK" if ok else "FEHLER"
    report.log(f"  Gesendet : {readable(probe)}")
    report.log(f"  Empfangen: {readable(rx)}")
    if ok:
        report.log("  -> PC, Adapter und Kabel bis zur Bruecke funktionieren.")
    else:
        report.log("  -> Adapter/Treiber/Kabel defekt oder Bruecke sitzt nicht richtig.")


def test_standard(port, report, data):
    report.section(f"3) Standardtest wie Messskript ({SCRIPT_BAUDRATE} Baud, {SCRIPT_FORMAT}, Adresse {SCRIPT_ADDR})")
    results = []
    try:
        ser = open_port(port)
    except serial.SerialException as e:
        report.log(f"  FEHLER: {e}")
        data["standard"] = [{"status": "Port-Fehler", "fehler": str(e)}]
        return False
    try:
        for command in ("*idn?", "fetc?"):
            report.log(f"\n  Befehl {command}:")
            results.append(transact(ser, SCRIPT_ADDR, command, report=report))
            time.sleep(0.2)
    finally:
        ser.close()
    data["standard"] = results
    return results[0]["status"] == "OK"


VARIANTS = [
    ("DTR/RTS aus", dict(dtr=False, rts=False)),
    ("Hardware-Handshake RTS/CTS", dict(rtscts=True)),
    ("Langsames Senden (5 ms/Byte)", dict(slow=True)),
    ("Mit Blockcheck (BLOCKCHECK am Geraet = ON)", dict(with_bcc=True)),
]


def test_variants(port, report, data):
    report.section("4) Varianten bei Standard-Einstellung")
    results = {}
    for label, kwargs in VARIANTS:
        r = quick_probe(port, SCRIPT_BAUDRATE, SCRIPT_FORMAT, SCRIPT_ADDR, **kwargs)
        results[label] = r
        extra = f" -> '{r['antwort']}'" if r.get("antwort") else ""
        report.log(f"  {label:<45} {r['status']}{extra}   RX: {r.get('rx_select', r.get('fehler', ''))}")
    data["varianten"] = results


def test_address_scan(port, report, data, baud=SCRIPT_BAUDRATE, fmt=SCRIPT_FORMAT):
    report.section(f"5) Adress-Scan ({baud} Baud, {fmt}, {len(SCAN_ADDRS)} Adressen)")
    hits = []
    for addr in SCAN_ADDRS:
        r = quick_probe(port, baud, fmt, addr)
        if r["status"] == "Port-Fehler":
            report.log(f"  Port-Fehler: {r.get('fehler')}")
            break
        if r["status"] != "keine Antwort":
            extra = f" -> '{r['antwort']}'" if r.get("antwort") else ""
            report.log(f"  Adresse {addr}: {r['status']}{extra}   RX: {r['rx_select']}")
            hits.append({"adresse": addr, **r})
    if not hits:
        report.log("  Keine Adresse hat geantwortet.")
    data["adress_scan"] = hits
    return hits


def test_serial_scan(port, report, data, addrs):
    report.section(f"6) Scan Baudrate x Datenformat (Adressen: {', '.join(addrs)})")
    hits = []
    total = len(SCAN_BAUDS) * len(SCAN_FORMATS) * len(addrs)
    done = 0
    for baud in SCAN_BAUDS:
        for fmt in SCAN_FORMATS:
            for addr in addrs:
                done += 1
                print(f"\r  ... {done}/{total}  {baud} {fmt} {addr}   ", end="", flush=True)
                r = quick_probe(port, baud, fmt, addr)
                if r["status"] == "Port-Fehler":
                    print()
                    report.log(f"  {baud:>6} Baud {fmt}: vom Adapter nicht unterstuetzt ({r['fehler']})")
                    continue
                if r["status"] == "keine Antwort":
                    continue
                print()
                extra = f" -> '{r['antwort']}'" if r.get("antwort") else ""
                report.log(f"  {baud:>6} Baud {fmt}  Adresse {addr}: {r['status']}{extra}   RX: {r['rx_select']}")
                hits.append({"baud": baud, "format": fmt, "adresse": addr, **r})
    print()
    if not hits:
        report.log("  Bei keiner Kombination kam irgendeine Antwort.")
    data["serial_scan"] = hits
    return hits


# ---------------------------------------------------------
# Auswertung
# ---------------------------------------------------------
def evaluate(report, data):
    report.section("AUSWERTUNG")
    findings = []

    if not data.get("port_offen"):
        findings.append("Der COM-Port laesst sich nicht oeffnen. Pruefen: richtiger Port gewaehlt? "
                        "Messskript oder anderes Programm (z.B. Hersteller-Software) noch offen? "
                        "Adapter im Geraetemanager mit Warnsymbol?")
        data["diagnose"] = findings
        for f in findings:
            report.log(f"* {f}")
        return

    standard = data.get("standard", [])
    if standard and standard[0].get("status") == "OK":
        findings.append(f"Kommunikation mit den Skript-Einstellungen funktioniert. Geraet meldet: "
                        f"'{standard[0]['antwort']}'.")
        fetch = standard[1] if len(standard) > 1 else {}
        if fetch.get("status") != "OK":
            findings.append(f"ABER: Der Messwert-Befehl 'fetc?' liefert keinen Wert (Status: "
                            f"{fetch.get('status')}). Das Geraet ist erreichbar, gibt aber keinen "
                            "Messwert aus. Pruefen: Geraet im Messmodus? Triggerart (intern/extern/"
                            "Bus)? Laeuft eine Messung? Einstellungen mit dem funktionierenden Geraet "
                            "im Menue vergleichen.")
        else:
            findings.append(f"'fetc?' liefert '{fetch.get('antwort')}'. Wenn das Messskript trotzdem "
                            "nicht geht: im Messskript den gleichen COM-Port waehlen und pruefen, ob "
                            "das Messwertformat (Zahl + Einheit) gelesen werden kann.")
        data["diagnose"] = findings
        for f in findings:
            report.log(f"* {f}")
        return

    # Funktionierende Variante bei Standard-Baudrate?
    for label, r in data.get("varianten", {}).items():
        if r.get("status") == "OK":
            findings.append(f"Kommunikation klappt NUR mit der Variante '{label}'. Das ist der "
                            "Unterschied zum funktionierenden Geraet - Geraete-Einstellung angleichen "
                            "oder Messskript anpassen.")

    working = [h for h in data.get("adress_scan", []) + data.get("serial_scan", []) if h.get("status") == "OK"]
    for h in working:
        diffs = []
        if h.get("adresse", SCRIPT_ADDR) != SCRIPT_ADDR:
            diffs.append(f"Adresse {h['adresse']} statt {SCRIPT_ADDR}")
        if h.get("baud", SCRIPT_BAUDRATE) != SCRIPT_BAUDRATE:
            diffs.append(f"Baudrate {h['baud']} statt {SCRIPT_BAUDRATE}")
        if h.get("format", SCRIPT_FORMAT) != SCRIPT_FORMAT:
            diffs.append(f"Datenformat {h['format']} statt {SCRIPT_FORMAT}")
        if diffs:
            findings.append("GEFUNDEN: Geraet antwortet mit " + ", ".join(diffs) + f" (Antwort: "
                            f"'{h.get('antwort')}'). Am Geraet im RS232-Menue auf die Werte des "
                            "funktionierenden Geraets zuruecksetzen.")
    if working or findings:
        data["diagnose"] = findings
        for f in findings:
            report.log(f"* {f}")
        return

    all_results = (standard + list(data.get("varianten", {}).values())
                   + data.get("adress_scan", []) + data.get("serial_scan", []))
    statuses = {r.get("status") for r in all_results}

    if "NAK" in statuses:
        findings.append("Das Geraet antwortet mit NAK: Es hoert zu (Verkabelung, Baudrate und Adresse "
                        "stimmen), lehnt das Telegramm aber ab. Haeufigste Ursache: BLOCKCHECK steht am "
                        "Geraet auf ON (Menue 150) - auf OFF stellen. Sonst: Geraet im Local/Remote-"
                        "Sperrzustand oder anderes Protokoll eingestellt.")
    if "ACK, aber keine Antwort auf Poll" in statuses:
        findings.append("Das Geraet bestaetigt den Befehl (ACK), liefert beim Abfragen aber keine Daten. "
                        "Pruefen: Protokoll/Abschlusszeichen am Geraet, Firmware-Version mit dem "
                        "funktionierenden Geraet vergleichen.")
    if "Echo" in statuses:
        findings.append("Es kommen die eigenen gesendeten Zeichen zurueck (Echo). Ursache: Pin 2 und 3 "
                        "im Kabel kurzgeschlossen, RS485-Adapter mit Echo, oder Echo-Funktion am Geraet. "
                        "Kabel und Adapter mit dem funktionierenden Geraet tauschen.")
    if "unlesbar" in statuses:
        findings.append("Es kommen Zeichen zurueck, aber unlesbar: typisch fuer falsche Baudrate oder "
                        "falsches Datenformat (Paritaet/Datenbits). Baudrate/Format am Geraet mit dem "
                        "funktionierenden Geraet vergleichen.")
    if not findings:
        findings.append("Das Geraet antwortet bei KEINER Einstellung (kein einziges Byte). Damit ist es "
                        "fast sicher ein Verbindungs- bzw. Schnittstellenproblem, keine Einstellungssache:")
        findings.append("  a) Kabel: 1:1-Kabel vs. Nullmodem-Kabel (gekreuzt). Das KABEL vom "
                        "funktionierenden Geraet an diesem Geraet testen - das ist der schnellste Test.")
        findings.append("  b) Am Geraet ist evtl. eine andere Schnittstelle aktiv (USB/RS485/"
                        "Feldbus statt RS232) oder die RS232-Option fehlt/ist defekt.")
        findings.append("  c) USB-Seriell-Adapter/Treiber: Adapter vom funktionierenden Geraet "
                        "verwenden und mit --loopback testen.")
        findings.append("  d) Falscher COM-Port gewaehlt (bei mehreren Adaptern Port im Geraetemanager "
                        "pruefen: Adapter abstecken und schauen, welcher Port verschwindet).")
        lines = data.get("steuerleitungen", {})
        if not any(lines.values()):
            findings.append("  Zusatz: Keine Steuerleitung (DSR/CTS) aktiv - spricht fuer Kabel nicht "
                            "angeschlossen, Geraet aus, oder 3-Draht-Kabel. Mit dem funktionierenden "
                            "Geraet vergleichen.")

    data["diagnose"] = findings
    for f in findings:
        report.log(f"* {f}")


# ---------------------------------------------------------
# Vergleich mehrerer Berichte
# ---------------------------------------------------------
def summarize(d):
    pi = d.get("port_info") or {}
    standard = d.get("standard") or [{}]
    working = [h for h in d.get("adress_scan", []) + d.get("serial_scan", []) if h.get("status") == "OK"]
    work_txt = "; ".join(f"{h.get('baud', SCRIPT_BAUDRATE)} {h.get('format', SCRIPT_FORMAT)} "
                         f"Adr {h.get('adresse')}" for h in working[:3]) or "-"
    variants = ", ".join(f"{k}" for k, v in d.get("varianten", {}).items() if v.get("status") == "OK") or "-"
    lines = d.get("steuerleitungen") or {}
    return {
        "Port": pi.get("device", d.get("port")),
        "Adapter": pi.get("beschreibung"),
        "VID:PID": f"{pi.get('vid')}:{pi.get('pid')}" if pi.get("vid") else "-",
        "Port offen": d.get("port_offen"),
        "CTS/DSR/CD": "/".join("1" if lines.get(k) else "0" for k in ("CTS", "DSR", "CD")),
        "Standard *idn?": standard[0].get("status"),
        "RX *idn?": standard[0].get("rx_select"),
        "Geraete-ID": standard[0].get("antwort") or "-",
        "fetc?": standard[1].get("status") if len(standard) > 1 else "-",
        "Varianten OK": variants,
        "Gefundene Einstellung": work_txt,
        "Loopback": d.get("loopback", "nicht getestet"),
    }


def compare(files):
    reports = []
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        reports.append((d.get("name") or Path(f).stem, summarize(d)))
    keys = list(reports[0][1].keys())
    width = max(len(k) for k in keys) + 2
    col = 32
    print("".ljust(width) + "".join(name[:col - 2].ljust(col) for name, _ in reports))
    print("-" * (width + col * len(reports)))
    for k in keys:
        values = [str(s[k]) for _, s in reports]
        marker = "  <-- unterschiedlich" if len(set(values)) > 1 else ""
        print(k.ljust(width) + "".join(v[:col - 2].ljust(col) for v in values) + marker)


# ---------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------
def choose_port():
    ports = list(serial.tools.list_ports.comports())
    print("Verfuegbare COM-Ports:")
    for i, p in enumerate(ports, 1):
        print(f"  {i}) {p.device}  -  {p.description}")
    if not ports:
        print("  (keine gefunden)")
    choice = input("Nummer oder Portname (z.B. COM3): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(ports):
        return ports[int(choice) - 1].device
    return choice


def main():
    parser = argparse.ArgumentParser(description="RESISTOMAT 2316 RS232-Diagnose")
    parser.add_argument("--port", help="COM-Port, z.B. COM3")
    parser.add_argument("--name", help="Name des Geraets fuer den Bericht")
    parser.add_argument("--voll", action="store_true", help="kombinierter Scan Adresse x Baud x Format")
    parser.add_argument("--loopback", action="store_true", help="Loopback-Test Kabel/Adapter")
    parser.add_argument("--vergleich", nargs="+", metavar="JSON", help="Berichte vergleichen")
    args = parser.parse_args()

    if args.vergleich:
        compare(args.vergleich)
        return

    interactive = not args.port
    port = args.port or choose_port()
    if not port:
        return
    name = args.name or (input("Name des Geraets (z.B. Geraet 2): ").strip() if interactive else "") or port

    report = Report()
    data = {"name": name, "port": port, "zeit": datetime.datetime.now().isoformat(timespec="seconds"),
            "skript_einstellungen": {"baud": SCRIPT_BAUDRATE, "format": SCRIPT_FORMAT, "adresse": SCRIPT_ADDR}}
    report.log(f"RESISTOMAT 2316 Diagnose - {name} - {port} - {data['zeit']}")

    test_port_info(port, report, data)
    if test_open_and_lines(port, report, data):
        if args.loopback:
            test_loopback(port, report, data)
        elif test_standard(port, report, data):
            pass  # funktioniert -> Scans nicht noetig
        else:
            test_variants(port, report, data)
            if args.voll:
                test_serial_scan(port, report, data, SCAN_ADDRS)
            else:
                hits = test_address_scan(port, report, data)
                addrs = list(dict.fromkeys([SCRIPT_ADDR] + [h["adresse"] for h in hits]))
                if not any(h["status"] == "OK" for h in hits):
                    test_serial_scan(port, report, data, addrs)

    if not args.loopback:
        evaluate(report, data)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    base = Path.cwd() / f"Resistomat_Diagnose_{safe_name}_{stamp}"
    base.with_suffix(".txt").write_text("\n".join(report.lines), encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nBericht gespeichert:\n  {base.with_suffix('.txt')}\n  {base.with_suffix('.json')}")

    if interactive:
        input("\nEnter zum Beenden...")


if __name__ == "__main__":
    main()
