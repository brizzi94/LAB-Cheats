"""
Keysight 34461A -> DUT-Serienmessung mit automatischer Stabilitaetserkennung
(Probe-Hold-Nachbildung)
=============================================================================

Einmal am Anfang die Anzahl der DUTs eingeben, danach laeuft alles
automatisch: das Skript wartet auf einen stabilen Messwert (= Prueftip
sitzt sauber an), erfasst ihn, piept zur Bestaetigung, wartet dann auf
eine deutliche Wertaenderung (= Kontakt geloest / naechstes DUT angesetzt)
und geht automatisch zum naechsten DUT ueber. Dadurch bleiben beide Haende
fuer das Messen frei.

Die CSV hat drei Spalten: "ZEIT" (Stunde.Minute), "DUT" (Nummer) und "mV".
Jedes gemessene DUT ist eine eigene Zeile. Trennzeichen ist Semikolon
(nicht Komma), damit Excel mit deutscher Spracheinstellung die Datei
automatisch in Spalten aufteilt statt alles in eine Zelle zu packen.

Voraussetzungen:
    pip install pyvisa pyvisa-py pyusb
"""

import csv
import time
import winsound
from datetime import datetime

import pyvisa


# ------------------- KONFIGURATION -------------------

INSTRUMENT_IP = '169.254.4.61'

MEASURE_FUNCTION = 'CONF:VOLT:DC AUTO'  # ggf. anpassen, z.B. 'CONF:RES AUTO'

OUTPUT_CSV = 'messungen.csv'

STABIL_TOLERANZ = 0.001    # V, wie stark der Wert schwanken darf, um als "stabil" zu gelten
STABIL_DAUER = 5           # Anzahl aufeinanderfolgender stabiler Messungen
RESET_SCHWELLE = 0.05      # V, Abweichung vom letzten Wert, ab der ein "neuer Kontakt" erkannt wird
POLL_INTERVALL = 0.15      # s zwischen den Messungen

# -------------------------------------------------------


def connect_instrument():
    rm = pyvisa.ResourceManager('@py')

    # Erst Raw-Socket (Port 5025) versuchen, bei Fehlschlag auf
    # VXI-11 ('inst0::INSTR') zurueckfallen.
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


def piep(erfolg=True):
    if erfolg:
        winsound.Beep(1500, 150)
    else:
        winsound.Beep(600, 300)


def warte_auf_stabilen_wert(inst):
    letzte_werte = []
    while True:
        wert = float(inst.query('READ?'))
        letzte_werte.append(wert)
        if len(letzte_werte) > STABIL_DAUER:
            letzte_werte.pop(0)
        if len(letzte_werte) == STABIL_DAUER and (max(letzte_werte) - min(letzte_werte)) < STABIL_TOLERANZ:
            return sum(letzte_werte) / len(letzte_werte)
        time.sleep(POLL_INTERVALL)


def warte_auf_kontaktwechsel(inst, letzter_wert):
    while True:
        wert = float(inst.query('READ?'))
        if abs(wert - letzter_wert) > RESET_SCHWELLE:
            return
        time.sleep(POLL_INTERVALL)


def main():
    try:
        inst = connect_instrument()
    except Exception as e:
        print(f"Verbindung fehlgeschlagen: {e}")
        return

    anzahl = int(input("Anzahl DUTs: "))

    with open(OUTPUT_CSV, 'w', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(['ZEIT', 'DUT', 'mV'])

        print("\nMessung startet - Pruefspitzen ansetzen...")

        for i in range(1, anzahl + 1):
            voltage = warte_auf_stabilen_wert(inst)
            ts = datetime.now().strftime("%H.%M")
            mv = round(voltage * 1000, 2)

            writer.writerow([ts, i, mv])
            f.flush()

            piep(True)
            print(f"DUT {i}/{anzahl}: {mv:.2f} mV")

            if i < anzahl:
                warte_auf_kontaktwechsel(inst, voltage)

    inst.close()
    piep(True)
    piep(True)
    print(f"\nAlle {anzahl} DUTs gemessen. Ergebnisse in {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
