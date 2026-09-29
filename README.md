# Messtools

Kleine Python-Skripte für die Arbeit im Labor.

| Skript | Zweck | Abhängigkeiten |
|---|---|---|
| `Watchdog_2.0.py` | Überwacht einen Ordner (auch auf Netzlaufwerken) und meldet neue Dateien in der Konsole bzw. als Desktop-Benachrichtigung. | `watchdog`, optional `plyer` |
| `Watchdog_2.1_GUI.py` | Wie 2.0, zeigt neue Dateien zusätzlich live in einem Fenster (tkinter). | `watchdog`, optional `plyer` |
| `Resistomat_2316_Messung.py` | Liest Messwerte vom RESISTOMAT 2316 über RS232 und schreibt bestätigte Werte mit Zeitstempel in eine Excel-Datei. | `pyserial`, `openpyxl` |
| `z_plus_mvp.py` | Fährt am Netzgerät TDK-Lambda Z+ 20V/20A ein Stromprofil (Stufen über die Zeit, auch mehrfach wiederholt) und loggt Spannung und Strom in eine CSV-Datei. | `pyvisa`, `pyvisa-py`, `pyserial` |

## Installation

```
pip install watchdog plyer pyserial openpyxl pyvisa pyvisa-py
```

## Konfiguration

- **Watchdog:** Den zu überwachenden Ordner (`ZIELORDNER`), Dateiendungen und das Abfrageintervall oben im Skript anpassen.
- **Resistomat:** Die `BAUDRATE` im Skript an die Einstellung des Geräts anpassen. Am Gerät muss im Menü 150 (RS232) `BLOCKCHECK` auf `OFF` stehen. COM-Port und Speicherort werden beim Start abgefragt.
- **Z+ Netzgerät:** Nur den Block `EINSTELLUNGEN` oben im Skript anpassen: COM-Port, Baudrate und Adresse (am Gerät unter `REM`), Spannungsgrenze sowie das Stromprofil (`PROFIL_TEST` bzw. `PROFIL_ECHT`). Abbruch mit Strg+C, der Ausgang wird dabei immer abgeschaltet.
