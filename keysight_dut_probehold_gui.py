"""
Keysight 34461A -> DUT-Serienmessung mit automatischer Stabilitaetserkennung
(Probe-Hold-Nachbildung) -- GUI-Version
=============================================================================

Einmal am Anfang die Anzahl der DUTs eingeben, danach laeuft alles
automatisch: das Skript wartet auf einen stabilen Messwert (= Prueftip
sitzt sauber an), erfasst ihn, piept zur Bestaetigung, wartet dann auf
eine deutliche Wertaenderung (= Kontakt geloest / naechstes DUT angesetzt)
und geht automatisch zum naechsten DUT ueber. Dadurch bleiben beide Haende
fuer das Messen frei.

Die CSV hat drei Spalten: "ZEIT" (Stunde.Minute), "DUT" (Nummer) und "mV".
Jedes gemessene DUT ist eine eigene Zeile.

Voraussetzungen:
    pip install pyvisa pyvisa-py pyusb
"""

import csv
import threading
import time
import tkinter as tk
import tkinter.ttk as ttk
import winsound
from datetime import datetime
from tkinter import messagebox

import pyvisa


# ------------------- STANDARDKONFIGURATION -------------------
DEFAULT_IP = '169.254.4.61'
DEFAULT_FUNCTION = 'CONF:VOLT:DC AUTO'
DEFAULT_OUTPUT_CSV = 'messungen.csv'
DEFAULT_STABIL_TOLERANZ = 0.001
DEFAULT_STABIL_DAUER = 5
DEFAULT_RESET_SCHWELLE = 0.05
DEFAULT_POLL_INTERVALL = 0.15


# ---------------------------------------------------------------

class MeasurementCancelled(Exception):
    """Wird ausgeloest, wenn der Benutzer waehrend einer Wartephase abbricht."""


def connect_instrument(ip, measure_function):
    rm = pyvisa.ResourceManager('@py')
    versuche = [
        f'TCPIP0::{ip}::5025::SOCKET',
        f'TCPIP0::{ip}::inst0::INSTR',
    ]

    letzter_fehler = None
    for resource in versuche:
        try:
            inst = rm.open_resource(resource)
            inst.timeout = 5000
            inst.read_termination = '\n'
            inst.write_termination = '\n'
            idn = inst.query('*IDN?').strip()
            inst.write(measure_function)
            return inst, f"Verbunden ueber {resource}\nVerbunden: {idn}"
        except Exception as e:
            letzter_fehler = e

    raise letzter_fehler


def piep(erfolg=True):
    if erfolg:
        winsound.Beep(1500, 150)
    else:
        winsound.Beep(600, 300)


def warte_auf_stabilen_wert(inst, stabil_toleranz, stabil_dauer, poll_intervall, stop_event):
    letzte_werte = []
    while True:
        if stop_event.is_set():
            raise MeasurementCancelled()
        wert = float(inst.query('READ?'))
        letzte_werte.append(wert)
        if len(letzte_werte) > stabil_dauer:
            letzte_werte.pop(0)
        if len(letzte_werte) == stabil_dauer and (max(letzte_werte) - min(letzte_werte)) < stabil_toleranz:
            return sum(letzte_werte) / len(letzte_werte)
        time.sleep(poll_intervall)


def warte_auf_kontaktwechsel(inst, letzter_wert, reset_schwelle, poll_intervall, stop_event):
    while True:
        if stop_event.is_set():
            raise MeasurementCancelled()
        wert = float(inst.query('READ?'))
        if abs(wert - letzter_wert) > reset_schwelle:
            return
        time.sleep(poll_intervall)


def run_measurement(inst, config, log_callback, stop_event):
    """Fuehrt die Messreihe auf der bereits bestehenden Verbindung 'inst' aus.
    Oeffnet/schliesst keine eigene Verbindung -- das macht der Aufrufer."""
    try:
        log_callback("Messung startet - Pruefspitzen ansetzen...\n")

        with open(config['output_csv'], 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['ZEIT', 'DUT', 'mV'])

            for i in range(1, config['dut_anzahl'] + 1):
                voltage = warte_auf_stabilen_wert(
                    inst,
                    config['stabil_toleranz'],
                    config['stabil_dauer'],
                    config['poll_intervall'],
                    stop_event,
                )
                ts = datetime.now().strftime('%H.%M')
                mv = round(voltage * 1000, 2)

                writer.writerow([ts, i, mv])
                f.flush()

                piep(True)
                log_callback(f"DUT {i}/{config['dut_anzahl']}: {mv:.2f} mV\n")

                if i < config['dut_anzahl']:
                    warte_auf_kontaktwechsel(
                        inst,
                        voltage,
                        config['reset_schwelle'],
                        config['poll_intervall'],
                        stop_event,
                    )

        log_callback(f"\nAlle {config['dut_anzahl']} DUTs gemessen. Ergebnisse in {config['output_csv']}\n")
        piep(True)
        piep(True)

    except MeasurementCancelled:
        log_callback("Messung abgebrochen.\n")
    except Exception as exc:
        log_callback(f"Fehler: {exc}\n")
        piep(False)


class ProbeHoldApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Keysight DUT Probe-Hold')
        self.geometry('700x650')
        self.minsize(620, 540)

        self.instrument = None
        self.measurement_thread = None
        self.stop_event = threading.Event()

        self._build_ui()

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}
        container = ttk.Frame(self, padding=12)
        container.pack(fill='both', expand=True)

        fields = {
            'instrument_ip': ttk.Entry(container, width=28),
            'measure_function': ttk.Entry(container, width=28),
            'dut_anzahl': ttk.Entry(container, width=18),
            'output_csv': ttk.Entry(container, width=28),
            'stabil_toleranz': ttk.Entry(container, width=18),
            'stabil_dauer': ttk.Entry(container, width=18),
            'reset_schwelle': ttk.Entry(container, width=18),
            'poll_intervall': ttk.Entry(container, width=18),
        }

        defaults = {
            'instrument_ip': DEFAULT_IP,
            'measure_function': DEFAULT_FUNCTION,
            'dut_anzahl': '10',
            'output_csv': DEFAULT_OUTPUT_CSV,
            'stabil_toleranz': str(DEFAULT_STABIL_TOLERANZ),
            'stabil_dauer': str(DEFAULT_STABIL_DAUER),
            'reset_schwelle': str(DEFAULT_RESET_SCHWELLE),
            'poll_intervall': str(DEFAULT_POLL_INTERVALL),
        }

        for key, value in defaults.items():
            fields[key].insert(0, value)

        row = 0
        for label, key in [
            ('Instrument-IP', 'instrument_ip'),
            ('Messfunktion', 'measure_function'),
            ('DUT-Anzahl', 'dut_anzahl'),
            ('CSV-Datei', 'output_csv'),
        ]:
            ttk.Label(container, text=label).grid(row=row, column=0, sticky='w', **pad)
            fields[key].grid(row=row, column=1, sticky='ew', **pad)
            row += 1

        row += 1
        ttk.Label(container, text='Stabilitaetstoleranz [V]').grid(row=row, column=0, sticky='w', **pad)
        fields['stabil_toleranz'].grid(row=row, column=1, sticky='ew', **pad)
        ttk.Label(container, text='Stabilitaetsdauer').grid(row=row + 1, column=0, sticky='w', **pad)
        fields['stabil_dauer'].grid(row=row + 1, column=1, sticky='ew', **pad)
        ttk.Label(container, text='Reset-Schwelle [V]').grid(row=row + 2, column=0, sticky='w', **pad)
        fields['reset_schwelle'].grid(row=row + 2, column=1, sticky='ew', **pad)
        ttk.Label(container, text='Poll-Intervall [s]').grid(row=row + 3, column=0, sticky='w', **pad)
        fields['poll_intervall'].grid(row=row + 3, column=1, sticky='ew', **pad)

        button_row = row + 4
        self.connect_button = ttk.Button(container, text='Verbinden', command=self.connect_instrument_ui)
        self.connect_button.grid(row=button_row, column=0, sticky='ew', **pad)
        self.start_button = ttk.Button(container, text='Messung starten', command=self.start_measurement)
        self.start_button.grid(row=button_row, column=1, sticky='ew', **pad)
        self.cancel_button = ttk.Button(container, text='Abbrechen', command=self.cancel_measurement, state='disabled')
        self.cancel_button.grid(row=button_row, column=2, sticky='ew', **pad)

        self.status_var = tk.StringVar(value='Bereit')
        ttk.Label(container, textvariable=self.status_var, foreground='darkblue').grid(
            row=button_row + 1, column=0, columnspan=3, sticky='w', **pad
        )

        log_frame = ttk.LabelFrame(container, text='Log')
        log_frame.grid(row=button_row + 2, column=0, columnspan=3, sticky='nsew', **pad)
        container.grid_columnconfigure(1, weight=1)
        container.grid_rowconfigure(button_row + 2, weight=1)

        self.log_widget = tk.Text(log_frame, height=18, wrap='word', state='disabled')
        self.log_widget.pack(fill='both', expand=True, padx=8, pady=8)

        self.fields = fields

    def log(self, text):
        self.after(0, self._append_log, text)

    def _append_log(self, text):
        self.log_widget.configure(state='normal')
        self.log_widget.insert('end', text)
        self.log_widget.see('end')
        self.log_widget.configure(state='disabled')

    def connect_instrument_ui(self):
        try:
            config = self._get_config()
            inst, status = connect_instrument(config['instrument_ip'], config['measure_function'])
            self.instrument = inst
            self.status_var.set('Verbunden')
            self.log(f"{status}\n")
        except Exception as exc:
            self.instrument = None
            self.status_var.set('Verbindung fehlgeschlagen')
            messagebox.showerror('Verbindungsfehler', str(exc))
            self.log(f"Verbindung fehlgeschlagen: {exc}\n")

    def _get_config(self):
        try:
            return {
                'instrument_ip': self.fields['instrument_ip'].get().strip(),
                'measure_function': self.fields['measure_function'].get().strip(),
                'dut_anzahl': int(self.fields['dut_anzahl'].get()),
                'output_csv': self.fields['output_csv'].get().strip() or DEFAULT_OUTPUT_CSV,
                'stabil_toleranz': float(self.fields['stabil_toleranz'].get()),
                'stabil_dauer': int(self.fields['stabil_dauer'].get()),
                'reset_schwelle': float(self.fields['reset_schwelle'].get()),
                'poll_intervall': float(self.fields['poll_intervall'].get()),
            }
        except ValueError as exc:
            raise ValueError(f'Ungueltige Eingabe: {exc}')

    def cancel_measurement(self):
        self.stop_event.set()
        self.status_var.set('Abbruch angefordert')
        self.log('Abbruch angefordert...\n')

    def start_measurement(self):
        try:
            config = self._get_config()
        except ValueError as exc:
            messagebox.showerror('Eingabe fehlerhaft', str(exc))
            return

        if self.instrument is None:
            self.connect_instrument_ui()
            if self.instrument is None:
                # Verbindung ist fehlgeschlagen, Fehler wurde bereits angezeigt.
                return

        self.stop_event.clear()
        self.status_var.set('Messung laeuft')
        self.start_button.configure(state='disabled')
        self.cancel_button.configure(state='normal')

        self.measurement_thread = threading.Thread(
            target=self._measure_worker,
            args=(config,),
            daemon=True,
        )
        self.measurement_thread.start()

    def _measure_worker(self, config):
        try:
            run_measurement(self.instrument, config, self.log, self.stop_event)
        finally:
            if self.instrument is not None:
                try:
                    self.instrument.close()
                except Exception:
                    pass
                self.instrument = None
            self.after(0, lambda: self.start_button.configure(state='normal'))
            self.after(0, lambda: self.cancel_button.configure(state='disabled'))
            self.after(0, self.status_var.set, 'Bereit')


def main():
    app = ProbeHoldApp()
    app.mainloop()


if __name__ == '__main__':
    main()
