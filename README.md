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
| `keysight_34461a_web.py` + `keysight_34461a.html` | Fasst die Keysight-Skripte in einem Programm mit Browser-Oberfläche zusammen (Aufbau wie `Resistomat_V2.html`). Beim Start erscheint ein Menü: Modus (**Probe-Hold**, **Messwerte aufzeichnen**, **Einzelmessungen**), IP/Verbindungsart, Messfunktion, Messbereich, NPLC, Einheit, Ablauf-Parameter und CSV-Datei. Live-Wert, Verlaufsdiagramm mit Statistik, Piepton am PC und/oder Gerät. CSV mit Semikolon und Dezimalkomma, vorhandene Dateien werden weitergeführt. | `pyvisa`, `pyvisa-py` |

## Installation

```
pip install watchdog plyer pyserial openpyxl pyvisa pyvisa-py matplotlib
```

## Konfiguration

- **Watchdog:** Den zu überwachenden Ordner (`ZIELORDNER`), Dateiendungen und das Abfrageintervall oben im Skript anpassen.
- **Resistomat:** Die `BAUDRATE` im Skript an die Einstellung des Geräts anpassen. Am Gerät muss im Menü 150 (RS232) `BLOCKCHECK` auf `OFF` stehen. COM-Port und Speicherort werden beim Start abgefragt.
- **Z+ Netzgerät:** Nur den Block `EINSTELLUNGEN` oben im Skript anpassen: COM-Port, Baudrate und Adresse (am Gerät unter `REM`), Spannungsgrenze sowie das Stromprofil (`PROFIL_TEST` bzw. `PROFIL_ECHT`). Abbruch mit Strg+C, der Ausgang wird dabei immer abgeschaltet.
- **Keysight 34461A, Browser-Version (`keysight_34461a_web.py`):** Beide Dateien in denselben Ordner legen und das Skript starten, der Browser öffnet sich mit dem Menü (die .html direkt per Doppelklick zu öffnen funktioniert nicht, der Browser kann nicht selbst per LAN mit dem Gerät sprechen). Alle Einstellungen werden dort gemacht und beim nächsten Start wieder vorgeschlagen. Das Konsolenfenster muss offen bleiben, die Messung läuft darin weiter, auch wenn der Browser-Tab geschlossen wird.
- **Keysight 34461A (übrige Skripte):** Die Variable `INSTRUMENT_IP` (bzw. das Eingabefeld "Instrument-IP" in der GUI-Version) auf die aktuelle LAN-IP des Geräts anpassen (am Gerät unter `Utility → I/O Config → LAN Settings` ablesen). Alle Skripte verbinden sich zuerst über einen Raw-Socket (Port 5025) und fallen bei Fehlschlag automatisch auf VXI-11 zurück.
