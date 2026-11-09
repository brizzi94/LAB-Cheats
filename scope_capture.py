"""
scope_capture.py – Aktuelles Bild eines Teledyne LeCroy WaveSurfer erfassen

- liest alle eingeschalteten Kanäle (C1–C4, F1–F4) als Rohdaten (binär, voll aufgelöst)
- speichert sie als CSV (Zeit + Kanäle, Einheiten im Header, Trennzeichen ';')
- zeigt die Kurven als Plot und speichert ihn als PNG
- optional: Screenshot des Scope-Displays (--screenshot)

Installation (Windows):
    py -m pip install pyvisa pyvisa-py numpy matplotlib

Aufruf:
    py scope_capture.py                 -> IP aus SCOPE_IP unten
    py scope_capture.py 192.168.1.50    -> IP als Argument
    py scope_capture.py --screenshot    -> zusätzlich Display-Screenshot
    py scope_capture.py --no-plot       -> nur CSV + PNG, kein Plotfenster

Scope: Utilities -> Utilities Setup -> Remote -> LXI (VXI-11) aktivieren, IP ablesen.
"""

import argparse
import csv
import struct
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

SCOPE_IP = "192.168.1.50"          # <- anpassen
OUTPUT_DIR = Path("scope_captures")
CHANNELS = ["C1", "C2", "C3", "C4", "F1", "F2", "F3", "F4"]


# ---------------------------------------------------------------- Verbindung
def connect(ip):
    import pyvisa
    rm = pyvisa.ResourceManager("@py")
    scope = rm.open_resource(f"TCPIP0::{ip}::inst0::INSTR")
    scope.timeout = 30000
    scope.chunk_size = 1024 * 1024
    scope.write("COMM_HEADER OFF")
    scope.write("COMM_FORMAT DEF9,WORD,BIN")   # 16-bit Rohwerte
    scope.write("COMM_ORDER LO")               # little endian
    scope.write("WAVEFORM_SETUP SP,0,NP,0,FP,0,SN,0")  # alle Punkte
    print("Verbunden:", scope.query("*IDN?").strip())
    return scope


def active_channels(scope):
    active = []
    for ch in CHANNELS:
        try:
            if scope.query(f"{ch}:TRACE?").strip().upper().startswith("ON"):
                active.append(ch)
        except Exception:
            pass  # Kanal existiert auf diesem Modell nicht
    return active


# ---------------------------------------------------------------- Parsing
def _strip_block(raw):
    """Entfernt einen IEEE-488.2-Blockheader '#Nnnn…' falls vorhanden."""
    start = raw.find(b"#")
    if start < 0 or start > 16 or not raw[start + 1:start + 2].isdigit():
        return raw
    n_digits = int(raw[start + 1:start + 2])
    if n_digits == 0:                         # '#0' = unbestimmte Länge
        return raw[start + 2:]
    length = int(raw[start + 2:start + 2 + n_digits])
    return raw[start + 2 + n_digits:start + 2 + n_digits + length]


def _unit(desc, offset):
    return desc[offset:offset + 48].split(b"\x00")[0].decode(errors="ignore").strip()


def parse_waveform(raw):
    """Binärblock '#9nnnnnnnnn<WAVEDESC><Daten>' -> (t, y, info)."""
    block = _strip_block(raw)

    i = block.find(b"WAVEDESC")
    if i < 0:
        raise ValueError("Antwort enthält keinen WAVEDESC-Block")
    desc = block[i:]
    e = "<" if desc[34] == 1 else ">"           # COMM_ORDER: 1 = LOFIRST

    def lng(o): return struct.unpack(e + "i", desc[o:o + 4])[0]
    def flt(o): return struct.unpack(e + "f", desc[o:o + 4])[0]
    def dbl(o): return struct.unpack(e + "d", desc[o:o + 8])[0]
    def wrd(o): return struct.unpack(e + "h", desc[o:o + 2])[0]

    comm_type = wrd(32)                       # 0 = byte, 1 = word
    wave_desc_len = lng(36)
    user_text_len = lng(40)
    trigtime_len = lng(48)
    ris_len = lng(52)
    array1_len = lng(60)
    v_gain = flt(156)
    v_offset = flt(160)
    h_interval = flt(176)
    h_offset = dbl(180)

    data_start = wave_desc_len + user_text_len + trigtime_len + ris_len
    data = desc[data_start:data_start + array1_len]
    dtype = np.dtype(e + ("i2" if comm_type == 1 else "i1"))
    y_raw = np.frombuffer(data, dtype=dtype)

    y = y_raw * v_gain - v_offset
    t = h_offset + np.arange(len(y)) * h_interval
    info = {"v_unit": _unit(desc, 196) or "V", "h_unit": _unit(desc, 244) or "s",
            "dt": h_interval, "t0": h_offset, "points": len(y)}
    return t, y, info


def read_channel(scope, ch):
    scope.write(f"{ch}:WAVEFORM? ALL")
    return parse_waveform(scope.read_raw())


# ---------------------------------------------------------------- Ausgabe
def _same_time_axis(waves):
    """True, wenn alle Kurven dieselbe Zeitachse haben (Länge, dt, Start, Einheit)."""
    infos = [w[2] for w in waves.values()]
    ref = infos[0]
    return all(
        i["points"] == ref["points"] and i["h_unit"] == ref["h_unit"]
        and np.isclose(i["dt"], ref["dt"], rtol=1e-6, atol=0)
        and np.isclose(i["t0"], ref["t0"], rtol=0, atol=abs(ref["dt"]) * 1e-3)
        for i in infos
    )


def _write(path, header, cols):
    """Erste Spalte = Zeit (10 signifikante Stellen), übrige = Messwerte (7 Stellen,
    entspricht der float32-Genauigkeit des Scopes)."""
    fmt = ["%.10g"] + ["%.7g"] * (len(cols) - 1)
    text = [np.char.mod(f, c) for f, c in zip(fmt, cols)]
    with open(path, "w", newline="") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(header)
        wr.writerows(zip(*text))


def save_csv(path, waves):
    if _same_time_axis(waves):
        # gemeinsame Zeitachse -> eine Datei
        first = next(iter(waves.values()))
        header = [f"t [{first[2]['h_unit']}]"] + [f"{ch} [{w[2]['v_unit']}]" for ch, w in waves.items()]
        _write(path, header, [first[0]] + [w[1] for w in waves.values()])
        return [path]
    # unterschiedliche Zeitachsen (z. B. Zoom/Math/FFT) -> je Kanal eine Datei
    paths = []
    for ch, (t, y, info) in waves.items():
        p = path.with_name(f"{path.stem}_{ch}.csv")
        _write(p, [f"t [{info['h_unit']}]", f"{ch} [{info['v_unit']}]"], [t, y])
        paths.append(p)
    return paths


def time_scale(t, unit="s"):
    if unit != "s":                            # z. B. FFT in Hz -> nicht skalieren
        return 1, unit
    span = np.max(np.abs(t)) if len(t) else 1
    for factor, u in [(1e9, "ns"), (1e6, "µs"), (1e3, "ms"), (1, "s")]:
        if span * factor < 1000:
            return factor, u
    return 1, "s"


def plot(waves, png_path, show=True):
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    shared = _same_time_axis(waves)
    n = len(waves)
    fig, axes = plt.subplots(n, 1, figsize=(10, 2.4 * n + 0.8), sharex=shared, squeeze=False)
    for ax, (ch, (t, y, info)) in zip(axes[:, 0], waves.items()):
        k, tu = time_scale(t, info["h_unit"])
        ax.plot(t * k, y, lw=0.8)
        ax.set_ylabel(f"{ch} [{info['v_unit']}]")
        ax.grid(True, alpha=0.3)
        if info["h_unit"] == "s":
            ax.axvline(0, color="grey", lw=0.6, ls="--")  # Triggerzeitpunkt
        if len(y):
            ax.text(0.99, 0.95, f"min {y.min():.4g}  max {y.max():.4g}  ({info['points']} Pkt)",
                    transform=ax.transAxes, ha="right", va="top", fontsize=8)
        if not shared:
            ax.set_xlabel(f"[{tu}]")
    if shared:
        axes[-1, 0].set_xlabel(f"Zeit [{tu}]")
    fig.suptitle(png_path.stem, fontsize=10)
    fig.tight_layout()
    fig.savefig(png_path, dpi=150)
    if show:
        plt.show()


def screenshot(scope, path):
    scope.write("HARDCOPY_SETUP DEV,PNG,BCKG,WHITE,DEST,REMOTE,PORT,NET,AREA,FULLSCREEN")
    scope.write("SCREEN_DUMP")
    data = scope.read_raw()
    png = data.find(b"\x89PNG")
    path.write_bytes(data[png:] if png >= 0 else _strip_block(data))


# ---------------------------------------------------------------- Hauptablauf
def main():
    ap = argparse.ArgumentParser(description="LeCroy WaveSurfer: aktuelles Bild -> CSV + Plot")
    ap.add_argument("ip", nargs="?", default=SCOPE_IP)
    ap.add_argument("--screenshot", action="store_true", help="zusätzlich Display als PNG")
    ap.add_argument("--no-plot", action="store_true", help="kein Plotfenster öffnen")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = OUTPUT_DIR / f"capture_{stamp}"

    scope = connect(args.ip)

    # Erfassung anhalten, damit alle Kanäle aus derselben Aufnahme stammen
    trig_mode = scope.query("TRIG_MODE?").strip().upper()
    scope.write("STOP")
    try:
        chans = active_channels(scope)
        if not chans:
            sys.exit("Kein Kanal eingeschaltet.")
        waves = {}
        for ch in chans:
            waves[ch] = read_channel(scope, ch)
            print(f"  {ch}: {waves[ch][2]['points']} Punkte, dt = {waves[ch][2]['dt']:.3g} {waves[ch][2]['h_unit']}")

        if args.screenshot:
            try:
                screenshot(scope, base.with_name(base.name + "_screen.png"))
                print("Screenshot gespeichert.")
            except Exception as ex:
                print("Screenshot fehlgeschlagen:", ex)
    finally:
        if trig_mode in ("AUTO", "NORM", "NORMAL"):
            scope.write(f"TRIG_MODE {trig_mode}")   # vorherigen Modus wiederherstellen
        scope.close()

    for p in save_csv(base.with_suffix(".csv"), waves):
        print("CSV:", p)
    png = base.with_name(base.name + "_plot.png")
    plot(waves, png, show=not args.no_plot)
    print("Plot:", png)


if __name__ == "__main__":
    main()
