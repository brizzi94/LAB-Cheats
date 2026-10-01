"""
Keysight 34461A -> Trendmessung (Live-Plot + CSV-Log)
=======================================================

Liest kontinuierlich Messwerte, zeigt sie live als Plot an und
schreibt parallel JEDEN Wert in eine CSV. Laeuft unbestimmt lange,
bis das Plot-Fenster geschlossen oder mit Strg+C abgebrochen wird.

Die CSV hat zwei Spalten: "Zeit_s" und "Messwert". Jede Messung ist eine
eigene Zeile.

Fuer lange Laufzeiten (Stunden/ueber Nacht) ausgelegt:
    - CSV wird laufend geschrieben und regelmaessig geflusht,
      sodass bei einem Absturz/Stromausfall nur der letzte Wert fehlt
    - Der Live-Plot zeigt nur ein rollierendes Fenster der letzten
      N Punkte (sonst wird der Plot irgendwann langsam/speicherhungrig)
      -- die VOLLSTAENDIGEN Daten stehen trotzdem in der CSV

Voraussetzungen:
    pip install pyvisa pyvisa-py pyusb matplotlib
"""

import csv
import time
from collections import deque

import pyvisa
import matplotlib.pyplot as plt
import matplotlib.animation as animation


# ------------------- KONFIGURATION -------------------

INSTRUMENT_IP = '169.254.4.61'

MEASURE_FUNCTION = 'CONF:VOLT:DC AUTO'  # ggf. anpassen, z.B. 'CONF:RES AUTO'

OUTPUT_CSV = 'trend_messung.csv'

POLL_INTERVAL = 0.5  # Sekunden zwischen den Messungen

# Wie viele Punkte im Live-Plot gleichzeitig sichtbar sind
# (die CSV enthaelt trotzdem ALLE Werte, nicht nur diese)
PLOT_WINDOW_POINTS = 500

Y_AXIS_LABEL = 'Messwert'

# -------------------------------------------------------


def connect_instrument():
    rm = pyvisa.ResourceManager('@py')

    # Erst Raw-Socket (Port 5025) versuchen, bei Fehlschlag auf
    # VXI-11 ('inst0::INSTR') zurueckfallen -- je nach Geraet/Firmware
    # funktioniert mal der eine, mal der andere Weg zuverlaessiger.
    versuche = [
        f'TCPIP0::{INSTRUMENT_IP}::5025::SOCKET',
        f'TCPIP0::{INSTRUMENT_IP}::inst0::INSTR',
    ]

    letzter_fehler = None
    for resource in versuche:
        try:
            inst = rm.open_resource(resource)
            inst.timeout = 5000
            inst.read_termination = '\n'
            inst.write_termination = '\n'
            idn = inst.query('*IDN?').strip()
            print(f"Verbunden ueber {resource}")
            print(f"Verbunden: {idn}")
            inst.write(MEASURE_FUNCTION)
            return inst
        except Exception as e:
            print(f"Verbindung ueber {resource} fehlgeschlagen: {e}")
            letzter_fehler = e

    raise letzter_fehler


def main():
    try:
        inst = connect_instrument()
    except Exception as e:
        print(f"Verbindung fehlgeschlagen: {e}")
        return

    print(f"Logge nach: {OUTPUT_CSV}")
    print("Plot-Fenster schliessen oder Strg+C zum Beenden\n")

    csv_file = open(OUTPUT_CSV, 'w', newline='')
    writer = csv.writer(csv_file)
    writer.writerow(['Zeit_s', 'Messwert'])

    t0 = time.time()
    plot_t = deque(maxlen=PLOT_WINDOW_POINTS)
    plot_v = deque(maxlen=PLOT_WINDOW_POINTS)

    fig, ax = plt.subplots()
    line, = ax.plot([], [])
    ax.set_xlabel('Zeit [s]')
    ax.set_ylabel(Y_AXIS_LABEL)
    ax.set_title('Keysight 34461A - Trendmessung (live)')
    ax.grid(True)

    def poll_and_update(frame):
        try:
            val = float(inst.query('READ?'))
        except Exception as e:
            print(f"Lesefehler: {e}")
            return line,

        t = time.time() - t0

        writer.writerow([f"{t:.3f}", val])
        csv_file.flush()

        plot_t.append(t)
        plot_v.append(val)

        line.set_data(plot_t, plot_v)
        ax.relim()
        ax.autoscale_view()

        return line,

    ani = animation.FuncAnimation(
        fig, poll_and_update,
        interval=POLL_INTERVAL * 1000,
        cache_frame_data=False,
    )

    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        csv_file.close()
        inst.close()
        print(f"\nBeendet. Vollstaendige Daten liegen in: {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
