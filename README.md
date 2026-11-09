# Messtools

Kleine Python-Skripte für die Arbeit im Labor.

| Skript | Zweck | Abhängigkeiten |
|---|---|---|
| `Watchdog_2.0.py` | Überwacht einen Ordner (auch auf Netzlaufwerken) und meldet neue Dateien in der Konsole bzw. als Desktop-Benachrichtigung. | `watchdog`, optional `plyer` |
| `Watchdog_2.1_GUI.py` | Wie 2.0, zeigt neue Dateien zusätzlich live in einem Fenster (tkinter). | `watchdog`, optional `plyer` |
| `Resistomat_2316_Messung.py` | Liest Messwerte vom RESISTOMAT 2316 über RS232 und schreibt bestätigte Werte mit Zeitstempel in eine Excel-Datei. | `pyserial`, `openpyxl` |
| `z_plus_mvp.py` | Fährt am Netzgerät TDK-Lambda Z+ 20V/20A ein Stromprofil (Stufen über die Zeit, auch mehrfach wiederholt) und loggt Spannung und Strom in eine CSV-Datei. | `pyvisa`, `pyvisa-py`, `pyserial` |
| `keysight_dut_probehold.py` | Misst eine vorher festgelegte Anzahl DUTs nacheinander am Keysight 34461A (LAN), freihändig: erkennt einen stabilen Messwert automatisch als Auslöser (Probe-Hold-Nachbildung), piept zur Bestätigung und erkennt den Kontaktwechsel zum nächsten DUT selbst. Ergebnis in `messungen.csv` (`ZEIT`, `DUT`, `mV`, eine Zeile pro DUT). | `pyvisa`, `pyvisa-py` |
| `keysight_trend_logger.py` | Kontinuierliche Trendmessung am Keysight 34461A (LAN) mit Live-Plot und parallelem CSV-Log (`trend_messung.csv`, `Zeit_s`, `Messwert`, eine Zeile pro Messung). Für Langzeit-Monitoring eines Signals über Stunden. | `pyvisa`, `pyvisa-py`, `matplotlib` |
| `keysight_dut_probehold_gui.py` | Wie `keysight_dut_probehold.py`, mit grafischer Oberfläche (tkinter): IP, Messfunktion, DUT-Anzahl, CSV-Pfad und Stabilitätsparameter lassen sich in Feldern eintragen, Verbinden/Start/Abbrechen per Button, Log live im Fenster. | `pyvisa`, `pyvisa-py` |
| `scope_capture.py` | Holt das aktuelle Bild eines Teledyne LeCroy WaveSurfer (LAN, VXI-11): hält die Erfassung kurz an, liest alle eingeschalteten Kanäle (C1–C4, F1–F4) voll aufgelöst als Binärdaten, speichert sie als CSV (`;`-getrennt, Zeit + Kanäle mit Einheiten) und als Plot-PNG in `scope_captures/`. Optional `--screenshot` (Display als PNG) und `--no-plot`. | `pyvisa`, `pyvisa-py`, `numpy`, `matplotlib` |

## Installation

```
pip install watchdog plyer pyserial openpyxl pyvisa pyvisa-py numpy matplotlib
```

## Konfiguration

- **Watchdog:** Den zu überwachenden Ordner (`ZIELORDNER`), Dateiendungen und das Abfrageintervall oben im Skript anpassen.
- **Resistomat:** Die `BAUDRATE` im Skript an die Einstellung des Geräts anpassen. Am Gerät muss im Menü 150 (RS232) `BLOCKCHECK` auf `OFF` stehen. COM-Port und Speicherort werden beim Start abgefragt.
- **Z+ Netzgerät:** Nur den Block `EINSTELLUNGEN` oben im Skript anpassen: COM-Port, Baudrate und Adresse (am Gerät unter `REM`), Spannungsgrenze sowie das Stromprofil (`PROFIL_TEST` bzw. `PROFIL_ECHT`). Abbruch mit Strg+C, der Ausgang wird dabei immer abgeschaltet.
- **Keysight 34461A (alle drei Skripte):** Die Variable `INSTRUMENT_IP` (bzw. das Eingabefeld "Instrument-IP" in der GUI-Version) auf die aktuelle LAN-IP des Geräts anpassen (am Gerät unter `Utility → I/O Config → LAN Settings` ablesen). Alle Skripte verbinden sich zuerst über einen Raw-Socket (Port 5025) und fallen bei Fehlschlag automatisch auf VXI-11 zurück.
- **LeCroy WaveSurfer:** Am Scope unter `Utilities → Utilities Setup → Remote` LXI (VXI-11) aktivieren und die IP ablesen. Die IP entweder in `SCOPE_IP` oben im Skript eintragen oder als Argument übergeben (`py scope_capture.py 192.168.1.50`). War das Scope vorher in `AUTO`/`NORM`, wird dieser Triggermodus nach dem Auslesen wiederhergestellt; aus `SINGLE` bleibt es gestoppt.
