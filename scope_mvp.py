"""
scope_mvp.py – Minimaltest für Teledyne LeCroy WaveSurfer (vor scope_capture.py)

Prüft Schritt für Schritt, ob alles funktioniert:
  1. Verbindung (VXI-11) aufbauen
  2. *IDN? abfragen
  3. Triggermodus und eingeschaltete Kanäle abfragen
  4. eine Kurve (erster aktiver Kanal) binär lesen und auswerten
  5. Kurve kurz im Plotfenster anzeigen (mit --no-plot überspringen)

Es wird nichts gespeichert und die Erfassung wird nicht angehalten.

Installation (Windows):
    py -m pip install pyvisa pyvisa-py numpy matplotlib

Aufruf:
    py scope_mvp.py                 -> IP aus SCOPE_IP unten
    py scope_mvp.py 192.168.1.50    -> IP als Argument
    py scope_mvp.py --no-plot       -> ohne Plotfenster

Scope: Utilities -> Utilities Setup -> Remote -> LXI (VXI-11) aktivieren, IP ablesen.
"""

import argparse
import struct
import sys

SCOPE_IP = "192.168.1.50"          # <- anpassen
CHANNELS = ["C1", "C2", "C3", "C4", "F1", "F2", "F3", "F4"]
MAX_POINTS = 10000                 # für den Test reichen wenige Punkte


def step(nr, text):
    print(f"\n[{nr}] {text} ...")


def fail(text, hint=""):
    print(f"    FEHLER: {text}")
    if hint:
        print(f"    Tipp:   {hint}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="LeCroy WaveSurfer Verbindungstest")
    ap.add_argument("ip", nargs="?", default=SCOPE_IP)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    # 1 ---------------------------------------------------------- Verbindung
    step(1, f"Verbinde mit {args.ip} (VXI-11)")
    try:
        import pyvisa
        import numpy as np
    except ImportError as ex:
        fail(f"Paket fehlt: {ex.name}", "py -m pip install pyvisa pyvisa-py numpy matplotlib")
    try:
        rm = pyvisa.ResourceManager("@py")
        scope = rm.open_resource(f"TCPIP0::{args.ip}::inst0::INSTR")
        scope.timeout = 10000
    except Exception as ex:
        fail(ex, "IP prüfen (ping), gleiches Netz? Am Scope Remote -> LXI (VXI-11) aktiv?")
    print("    OK")

    try:
        # 2 ------------------------------------------------------ Identifikation
        step(2, "Frage *IDN? ab")
        try:
            scope.write("COMM_HEADER OFF")
            print("    OK:", scope.query("*IDN?").strip())
        except Exception as ex:
            fail(ex, "Verbindung steht, aber keine Antwort: Firewall / Remote-Einstellung prüfen.")

        # 3 ------------------------------------------------------ Status
        step(3, "Triggermodus und Kanäle")
        print("    Triggermodus:", scope.query("TRIG_MODE?").strip())
        active = []
        for ch in CHANNELS:
            try:
                state = scope.query(f"{ch}:TRACE?").strip()
            except Exception:
                state = "nicht vorhanden"
            print(f"    {ch}: {state}")
            if state.upper().startswith("ON"):
                active.append(ch)
        if not active:
            fail("kein Kanal eingeschaltet", "Am Scope mindestens einen Kanal einschalten.")

        # 4 ------------------------------------------------------ Kurve lesen
        ch = active[0]
        step(4, f"Lese Kurve von {ch} (max. {MAX_POINTS} Punkte)")
        scope.write("COMM_FORMAT DEF9,WORD,BIN")
        scope.write("COMM_ORDER LO")
        scope.write(f"WAVEFORM_SETUP SP,0,NP,{MAX_POINTS},FP,0,SN,0")
        scope.write(f"{ch}:WAVEFORM? ALL")
        raw = scope.read_raw()
        print(f"    {len(raw)} Bytes empfangen, Anfang: {raw[:12]!r}")

        i = raw.find(b"WAVEDESC")
        if i < 0:
            fail("kein WAVEDESC in der Antwort", "Firmware/Format prüfen, Ausgabe oben ansehen.")
        d = raw[i:]
        e = "<" if d[34] == 1 else ">"
        lng = lambda o: struct.unpack(e + "i", d[o:o + 4])[0]
        flt = lambda o: struct.unpack(e + "f", d[o:o + 4])[0]
        dbl = lambda o: struct.unpack(e + "d", d[o:o + 8])[0]
        start = lng(36) + lng(40) + lng(48) + lng(52)
        y_raw = np.frombuffer(d[start:start + lng(60)], dtype=e + "i2")
        y = y_raw * flt(156) - flt(160)
        dt, t0 = flt(176), dbl(180)
        t = t0 + np.arange(len(y)) * dt
        unit = d[196:244].split(b"\x00")[0].decode(errors="ignore") or "V"
        print(f"    OK: {len(y)} Punkte, dt = {dt:.3g} s, t0 = {t0:.3g} s")
        print(f"    min = {y.min():.4g} {unit}, max = {y.max():.4g} {unit}, Mittel = {y.mean():.4g} {unit}")
        print("    -> Werte mit der Anzeige am Scope vergleichen!")
    finally:
        scope.close()

    # 5 ---------------------------------------------------------- Plot
    if not args.no_plot:
        step(5, "Zeige Kurve (Fenster schließen zum Beenden)")
        import matplotlib.pyplot as plt
        plt.plot(t * 1e3, y, lw=0.8)
        plt.xlabel("Zeit [ms]")
        plt.ylabel(f"{ch} [{unit}]")
        plt.title(f"MVP-Test {ch}")
        plt.grid(True, alpha=0.3)
        plt.show()

    print("\nAlles OK – scope_capture.py kann verwendet werden.")


if __name__ == "__main__":
    main()
