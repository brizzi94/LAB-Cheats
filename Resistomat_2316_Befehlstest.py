"""
RESISTOMAT 2316 - Befehls-Testwerkzeug
---------------------------------------
Probiert verschiedene vermutete SCPI-Befehlsvarianten am echten Geraet
aus und protokolliert die Antworten. Ziel: herausfinden, welche
Schreibweise das Geraet tatsaechlich akzeptiert, damit das
Haupt-Messskript (Resistomat_2316_Messung.py) um Steuerbefehle
(Messbereich, Trigger, usw.) erweitert werden kann.

Nutzung:
1. Geraet per RS232 anschliessen, am Geraet Menue 150: BLOCKCHECK = OFF.
2. Dieses Skript starten (python Resistomat_2316_Befehlstest.py).
3. COM-Port auswaehlen, Rest laeuft automatisch durch.
4. Am Ende wird eine Protokolldatei "resistomat_befehlstest_<Zeit>.txt"
   im selben Ordner erzeugt - deren Inhalt bitte zurueckschicken.

Sicherheit:
- Zuerst werden nur LESENDE Abfragen (Befehle, die mit "?" enden)
  gesendet. Diese veraendern den Geraetezustand nicht.
- Zustandsveraendernde Befehle (Messung starten/stoppen) werden nur
  nach expliziter Bestaetigung getestet.
- *RST (Werkseinstellungen/Grundzustand) wird NICHT getestet.

Vorher pruefen:
- pip install pyserial
"""

import datetime
import time
import serial
import serial.tools.list_ports

BAUDRATE_DEFAULT = 9600

STX = b"\x02"
ETX = b"\x03"
ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
LF = b"\x0A"
EOT = b"\x04"
ADDR = b"0000"


def send(ser, command: str) -> bool:
    frame = EOT + ADDR + b"sr" + STX + command.encode("ascii") + LF + ETX
    ser.write(frame)
    return ser.read(1) == ACK


def poll(ser):
    ser.write(EOT + ADDR + b"po" + ENQ)
    raw = ser.read_until(ETX)
    if STX not in raw:
        return None
    ser.write(ACK)
    ser.read(1)  # abschliessendes EOT
    return raw.split(STX, 1)[1].split(ETX, 1)[0].decode("ascii", errors="replace").strip()


def try_command(ser, command: str, expect_response: bool):
    """Sendet einen Befehl, gibt (ack, antwort_oder_None, fehler_oder_None) zurueck."""
    try:
        ack = send(ser, command)
    except Exception as e:
        return False, None, f"Sende-Fehler: {e}"

    if not ack:
        return False, None, None

    if not expect_response:
        return True, None, None

    try:
        response = poll(ser)
    except Exception as e:
        return True, None, f"Poll-Fehler: {e}"

    return True, response, None


# Lesende Abfragen (sicher, veraendern den Geraetezustand nicht)
QUERY_COMMANDS = [
    ("Identifikation", "*idn?"),
    ("Selbsttest-Ergebnis", "*tst?"),
    ("Status-Byte", "*stb?"),
    ("Event-Status-Register", "*esr?"),
    ("SCPI-Fehlerwarteschlange", "syst:err?"),
    ("Letzter Messwert (bekannt funktionierend)", "fetc?"),
    ("Messbereich (Kurzform)", "sens:fres:rang?"),
    ("Messbereich (Langform)", "sense:fresistance:range?"),
    ("Auto-Range Status (Kurzform)", "sens:fres:rang:auto?"),
    ("Auto-Range Status (Langform)", "sense:fresistance:range:auto?"),
    ("Aufloesung", "sens:fres:res?"),
    ("Temperaturkompensation (kurz)", "sens:tcom?"),
    ("Temperaturkompensation Status", "sens:tcom:stat?"),
    ("Messstrom (Kurzform)", "sour:curr?"),
    ("Messstrom (Langform)", "sour:curr:lev:imm:ampl?"),
    ("Kontinuierliche Messung Status", "init:cont?"),
]

# Zustandsveraendernde Befehle (nur nach Bestaetigung)
ACTION_COMMANDS = [
    ("Status loeschen", "*cls"),
    ("Messung abbrechen", "abor"),
    ("Messung (neu) starten", "init"),
    ("Kontinuierliche Messung EIN", "init:cont on"),
    ("Kontinuierliche Messung AUS", "init:cont off"),
]


def pick_com_port():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    print("\nGefundene COM-Ports:")
    if ports:
        for i, p in enumerate(ports, 1):
            print(f"  {i}) {p}")
    else:
        print("  (keine automatisch erkannt)")

    choice = input("\nPort-Nummer eingeben oder COM-Namen direkt eintippen (z.B. COM4): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(ports):
        return ports[int(choice) - 1]
    return choice


def run_batch(ser, commands, expect_response, log_lines):
    for label, cmd in commands:
        ack, response, error = try_command(ser, cmd, expect_response)
        if error:
            status = f"FEHLER ({error})"
        elif not ack:
            status = "keine Antwort / NAK (Befehl vermutlich nicht unterstuetzt)"
        elif expect_response:
            status = f"OK -> {response!r}" if response is not None else "ACK, aber kein Messwert beim Poll"
        else:
            status = "ACK (Befehl angenommen)"

        line = f"{label:45s} | {cmd:35s} | {status}"
        print(line)
        log_lines.append(line)
        time.sleep(0.2)


def main():
    print("=== RESISTOMAT 2316 - Befehls-Testwerkzeug ===")
    port = pick_com_port()
    baud_input = input(f"Baudrate [{BAUDRATE_DEFAULT}]: ").strip()
    baudrate = int(baud_input) if baud_input else BAUDRATE_DEFAULT

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,
        )
    except serial.SerialException as e:
        print(f"Konnte {port} nicht oeffnen: {e}")
        return

    log_lines = [
        f"RESISTOMAT 2316 Befehlstest - {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Port: {port}, Baudrate: {baudrate}",
        "",
    ]

    print("\n--- Teste lesende Abfragen (sicher, veraendert nichts) ---")
    log_lines.append("--- Lesende Abfragen ---")
    run_batch(ser, QUERY_COMMANDS, expect_response=True, log_lines=log_lines)

    answer = input(
        "\nAuch zustandsveraendernde Befehle testen "
        "(Messung starten/stoppen, kontinuierlicher Modus)? [j/N]: "
    ).strip().lower()
    if answer == "j":
        print("\n--- Teste zustandsveraendernde Befehle ---")
        log_lines.append("")
        log_lines.append("--- Zustandsveraendernde Befehle ---")
        run_batch(ser, ACTION_COMMANDS, expect_response=False, log_lines=log_lines)

    ser.close()

    log_path = f"resistomat_befehlstest_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")

    print(f"\nFertig. Protokoll gespeichert als: {log_path}")
    print("Bitte den Inhalt dieser Datei zurueckschicken.")


if __name__ == "__main__":
    main()
