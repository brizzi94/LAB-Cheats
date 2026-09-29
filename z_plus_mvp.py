#!/usr/bin/env python3
"""TDK-Lambda Z+ 20V/20A (USB/COM, SCPI): Stromprofil fahren und Messwerte loggen.

Installation:  pip install pyvisa pyvisa-py pyserial
Start:         python z_plus_mvp.py
Abbruch:       Strg+C (der Ausgang wird in jedem Fall abgeschaltet)

Nur den Block "EINSTELLUNGEN" ändern.
"""
import csv
import ctypes
import sys
import time
from datetime import datetime

import pyvisa
from pyvisa.errors import VisaIOError

# ======================================================================
#  EINSTELLUNGEN
# ======================================================================

# --- Verbindung (am Gerät: REM -> INtF = USB, LanG = SCPI) ---
COM_PORT = 5            # COM-Nummer aus dem Geräte-Manager (COM5 -> 5)
BAUD = 57600            # muss zum Gerät passen (REM -> baUd)
ADRESSE = 6             # Geräteadresse (REM -> Adr)
TERMINATOR = "\r"       # Zeilenende, hat an diesem Gerät funktioniert

# --- Grenzen ---
SPANNUNGSGRENZE_V = 20.0   # Regelt das Gerät den Strom (CC), liegt die Spannung darunter.
                           # Strom kann nur bis Spannungsgrenze / Lastwiderstand fliessen.
                           # Nur so hoch wählen wie nötig (ohne Last liegt sie an den Klemmen).
STROM_MAX_A = 20.0         # Nennstrom des Geräts, Profilwerte darüber werden abgelehnt

# --- Messung und Ausgabe ---
# Jede Stufe bekommt automatisch das passende Intervall: Stufen bis STUFENDAUER_KURZ_S
# nutzen MESS_INTERVALL_KURZ_S, längere Stufen nutzen MESS_INTERVALL_S.
# Für ein einziges Intervall überall einfach beide Intervalle gleich setzen.
MESS_INTERVALL_S = 1.0        # Intervall für lange Stufen [s]
MESS_INTERVALL_KURZ_S = 0.2   # Intervall für kurze Stufen [s] (Grenze: 2 Abfragen brauchen ca. 0.1 s)
STUFENDAUER_KURZ_S = 600.0    # Stufen bis zu dieser Dauer [s] gelten als "kurz"
KONSOLE_ALLE_S = 10.0      # Abstand der Statuszeilen im Terminal
LOGDATEI = f"z_plus_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
BESTAETIGUNG = True        # True: vor dem Einschalten des Ausgangs Enter drücken
STANDBY_VERHINDERN = True  # True: Windows geht während des Laufs nicht in den Standby

# --- Profil: (Zeit in s, Strom in A). Der Wert gilt ab dieser Zeit (Stufen). ---
# Die erste Zeile muss bei 0 s beginnen, die letzte Zeit ist das Ende des Programms.
PROFIL_TEST = [
    (0.0, 1.0),
    (5.0, 1.5),
    (10.0, 1.5),
]
PROFIL_ECHT = [
    (0.0, 2.39),
    (15067.90, 2.39),
    (15067.91, 3.05),
    (17995.80, 3.05),
    (17995.81, 3.50),
    (43494.30, 3.50),
    (43494.31, 4.16),
    (45224.00, 4.16),
    (45224.01, 4.71),
    (45382.10, 4.71),
    (45382.11, 5.48),
    (45870.10, 5.48),
    (45870.11, 6.32),
    (45873.10, 6.32),
]   
PROFIL = PROFIL_ECHT       # <- hier zwischen PROFIL_TEST und PROFIL_ECHT umschalten
WIEDERHOLUNGEN = 2         # Anzahl Durchläufe des Profils (0 = endlos bis Strg+C)

# ======================================================================
#  Ab hier nichts ändern
# ======================================================================

RESOURCE = f"ASRL{COM_PORT}::INSTR"    # LAN statt USB: "TCPIP::<IP-Adresse>::INSTR"


def standby_verhindern(an):
    """Windows: kein Standby/Ruhezustand durch Leerlauf, solange das Skript läuft.
    Ändert keine Systemeinstellung und endet automatisch mit dem Prozess.
    Verhindert NICHT: Deckel zuklappen, Herunterfahren, Neustart, Abmelden."""
    if sys.platform != "win32":
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    funktion = ctypes.windll.kernel32.SetThreadExecutionState
    funktion.argtypes = [ctypes.c_uint]
    funktion(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if an else 0))


def pruefe_profil(profil):
    if not profil:
        raise ValueError("Profil ist leer.")
    if profil[0][0] != 0:
        raise ValueError(f"Profil: Zeile 1 muss bei 0 s beginnen (steht: {profil[0][0]} s).")
    for n, ((t1, _), (t2, _)) in enumerate(zip(profil, profil[1:]), start=2):
        if t2 <= t1:
            raise ValueError(f"Profil: Zeile {n} ({t2} s) liegt nicht nach Zeile {n - 1} ({t1} s). "
                             "Zeiten müssen streng ansteigen.")
    for n, (t, i) in enumerate(profil, start=1):
        if not 0 <= i <= STROM_MAX_A:
            raise ValueError(f"Profil: Zeile {n} ({t} s): Strom {i} A ausserhalb 0..{STROM_MAX_A} A.")


def aenderungen(profil):
    """Nur die Zeilen, in denen sich der Sollwert ändert."""
    stufen = [profil[0]]
    for t, i in profil[1:]:
        if i != stufen[-1][1]:
            stufen.append((t, i))
    return stufen


def sende(psu, befehl, pause=0.02):
    psu.write(befehl)
    time.sleep(pause)


def fehler_pruefen(psu):
    try:
        antwort = psu.query("SYST:ERR?").strip()
    except VisaIOError:
        print("Hinweis: keine Antwort auf SYST:ERR? (Fehlerabfrage übersprungen).")
        return
    if not antwort.startswith("0"):
        print("Geräte-Fehler:", antwort)


def intervalle_je_stufe(stufen, t_ende):
    """MESS_INTERVALL_KURZ_S für Stufen bis STUFENDAUER_KURZ_S, sonst MESS_INTERVALL_S."""
    intervalle = []
    for k, (t_start, _) in enumerate(stufen):
        t_stufenende = stufen[k + 1][0] if k + 1 < len(stufen) else t_ende
        dauer = t_stufenende - t_start
        intervalle.append(MESS_INTERVALL_KURZ_S if dauer <= STUFENDAUER_KURZ_S else MESS_INTERVALL_S)
    return intervalle


def fahre_profil(psu, f, log, durchlauf, t_gesamt0, zustand):
    """Ein Durchlauf des Profils. Zeit t beginnt bei jedem Durchlauf neu bei 0."""
    stufen = aenderungen(PROFIL)
    t_ende = PROFIL[-1][0]
    intervalle = intervalle_je_stufe(stufen, t_ende)
    soll = stufen[0][1]
    intervall = intervalle[0]
    naechste_stufe = 1
    naechste_messung = 0.0
    naechste_ausgabe = 0.0
    t0 = time.perf_counter()

    while (t := time.perf_counter() - t0) < t_ende:
        if naechste_stufe < len(stufen) and t >= stufen[naechste_stufe][0]:
            soll = stufen[naechste_stufe][1]          # Sollwert hat Vorrang
            sende(psu, f"CURR {soll}")
            print(f"t={t:8.2f} s  Sollwert -> {soll} A")
            intervall = intervalle[naechste_stufe]
            naechste_messung = t                       # sofort in dieser Stufe messen
            naechste_stufe += 1

        elif zustand["messen"] and t >= naechste_messung:
            try:
                v = float(psu.query("MEAS:VOLT?"))
                i = float(psu.query("MEAS:CURR?"))
                zustand["messfehler"] = 0
            except (VisaIOError, ValueError):
                v = i = ""
                zustand["messfehler"] += 1
                if zustand["messfehler"] >= 3:
                    zustand["messen"] = False
                    print("Messung abgeschaltet (3 Fehler in Folge), Profil läuft weiter.")
            t_gesamt = time.perf_counter() - t_gesamt0
            log.writerow([durchlauf, f"{t:.2f}", f"{t_gesamt:.2f}", soll, v, i])
            f.flush()
            naechste_messung += intervall

            if v != "":
                if t >= naechste_ausgabe:
                    print(f"t={t:8.1f} s  soll={soll} A  mess={v} V / {i} A")
                    naechste_ausgabe += KONSOLE_ALLE_S
                if v >= 0.98 * SPANNUNGSGRENZE_V and i < soll - 0.1 and not zustand["cv_gemeldet"]:
                    zustand["cv_gemeldet"] = True
                    print("Hinweis: Gerät regelt auf die Spannungsgrenze, der Strom liegt "
                          "unter dem Sollwert. Keine Last, Last zu hochohmig oder "
                          "SPANNUNGSGRENZE_V zu niedrig.")
        else:
            time.sleep(0.005)


def fahre_alle(psu, zustand):
    """Profil WIEDERHOLUNGEN-mal fahren (0 = endlos). Der Ausgang bleibt dazwischen an."""
    anzahl = WIEDERHOLUNGEN if WIEDERHOLUNGEN else "endlos"
    with open(LOGDATEI, "w", newline="", encoding="utf-8") as f:
        log = csv.writer(f, delimiter=";")
        log.writerow(["durchlauf", "zeit_s", "zeit_gesamt_s", "soll_A", "mess_V", "mess_A"])
        t_gesamt0 = time.perf_counter()
        n = 1
        while WIEDERHOLUNGEN == 0 or n <= WIEDERHOLUNGEN:
            zustand["durchlauf"] = n
            print(f"--- Durchlauf {n} von {anzahl} ---")
            if n > 1:
                sende(psu, f"CURR {PROFIL[0][1]}")     # zurück zum Startwert des Profils
            fahre_profil(psu, f, log, n, t_gesamt0, zustand)
            n += 1


def main():
    if not isinstance(WIEDERHOLUNGEN, int) or WIEDERHOLUNGEN < 0:
        raise ValueError("WIEDERHOLUNGEN muss eine ganze Zahl >= 0 sein (0 = endlos).")
    pruefe_profil(PROFIL)
    zustand = {"messen": True, "messfehler": 0, "cv_gemeldet": False, "durchlauf": 0}
    rm = pyvisa.ResourceManager()
    try:
        psu = rm.open_resource(RESOURCE, open_timeout=5000)
    except (VisaIOError, OSError) as e:
        raise SystemExit(
            f"Verbindung zu {RESOURCE} nicht möglich: {e}\n"
            "Meist belegt ein anderes Programm den Port (zweites Terminal, altes "
            "python.exe im Task-Manager, Waveform Creator, PuTTY) oder die COM-Nummer "
            "hat sich geändert (Geräte-Manager prüfen).")
    psu.timeout = 2000
    seriell = RESOURCE.startswith("ASRL")
    if seriell:
        psu.baud_rate = BAUD
        psu.write_termination = TERMINATOR
        psu.read_termination = TERMINATOR

    if STANDBY_VERHINDERN:
        standby_verhindern(True)
    try:
        if seriell:
            sende(psu, f"INST:NSEL {ADRESSE}")
        print("Verbunden:", psu.query("*IDN?").strip())

        dauer_s = PROFIL[-1][0]
        strom_max = max(i for _, i in PROFIL)
        if WIEDERHOLUNGEN == 0:
            durchlaeufe = "endlos (Abbruch mit Strg+C)"
        else:
            durchlaeufe = f"{WIEDERHOLUNGEN} ({dauer_s * WIEDERHOLUNGEN / 60:.1f} min gesamt)"
        print(f"Profil: {len(PROFIL)} Punkte, {dauer_s:.0f} s ({dauer_s / 60:.1f} min) pro Durchlauf, "
              f"max. {strom_max} A, Spannungsgrenze {SPANNUNGSGRENZE_V} V")
        print(f"Durchläufe: {durchlaeufe}, Log: {LOGDATEI}")
        if BESTAETIGUNG:
            input("Enter = Ausgang einschalten und starten (Strg+C = Abbruch) ")

        sende(psu, "*CLS", pause=0.3)               # *CLS braucht ca. 150 ms
        sende(psu, "SYST:ERR:ENAB")
        sende(psu, f"VOLT {SPANNUNGSGRENZE_V}")
        sende(psu, f"CURR {PROFIL[0][1]}")
        sende(psu, "OUTP:STAT ON")
        fehler_pruefen(psu)
        fahre_alle(psu, zustand)
        print("Alle Durchläufe beendet. Log:", LOGDATEI)
    except KeyboardInterrupt:
        print(f"Abbruch durch Benutzer (in Durchlauf {zustand['durchlauf']}).")
    finally:
        try:
            sende(psu, "OUTP:STAT OFF")
            fehler_pruefen(psu)
        finally:
            psu.close()
            if STANDBY_VERHINDERN:
                standby_verhindern(False)
            print("Ausgang aus, Verbindung geschlossen.")


if __name__ == "__main__":
    main()