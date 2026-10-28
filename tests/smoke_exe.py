"""
Testet die fertige Keysight_Messung.exe (bzw. das Skript) gegen ein
simuliertes 34461A auf 127.0.0.1:5025 - wird beim .exe-Build in GitHub
Actions ausgefuehrt.

Aufruf:  python tests/smoke_exe.py dist/Keysight_Messung.exe
         python tests/smoke_exe.py keysight_34461a_web.py
"""

import json
import os
import random
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = 'http://127.0.0.1:8765'
T0 = time.time()


class FakeDmm(socketserver.StreamRequestHandler):
    """Simuliert DUTs: 2 s stabiler Wert (je DUT 50 mV mehr), 1 s Spitzen abgehoben."""

    def handle(self):
        for raw in self.rfile:
            cmd = raw.decode().strip().upper()
            if cmd == '*IDN?':
                reply = 'Keysight Technologies,34461A,FAKE,1.0'
            elif cmd == 'SYST:ERR?':
                reply = '+0,"No error"'
            elif cmd == 'CONF?':
                reply = '"VOLT +1.000000E+01,+3.000000E-06"'
            elif cmd.endswith('NPLC?'):
                reply = '+1.00000000E+01'
            elif cmd == 'READ?':
                time.sleep(0.05)
                el = time.time() - T0
                if el % 3 < 2:
                    v = 0.1 + 0.05 * int(el // 3) + random.uniform(-2e-4, 2e-4)
                else:
                    v = random.uniform(-0.03, 0.03)
                reply = f'{v:+.8E}'
            else:
                continue
            self.wfile.write((reply + '\n').encode())


def api(path, body=None):
    if body is None:
        req = Request(BASE + path)
    else:
        req = Request(BASE + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def wait_for(cond, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        s = api('/api/state?since=0&ver=-1')
        if cond(s):
            return s
        time.sleep(0.3)
    raise AssertionError(f'Zeitueberschreitung: {what} (Status: {s["status"]})')


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print('OK  ', msg)


def main():
    target = sys.argv[1]
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 5025), FakeDmm)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cmd = [target] if target.endswith('.exe') else [sys.executable, target]
    env = dict(os.environ, KEYSIGHT_NO_BROWSER='1')
    app = subprocess.Popen(cmd, env=env)
    tmp = Path(tempfile.mkdtemp())
    try:
        for _ in range(60):
            try:
                meta = api('/api/meta')
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise AssertionError('Programm startet nicht')
        check('VOLT:DC' in meta['functions'], 'Menue-Daten (/api/meta)')
        with urlopen(BASE + '/', timeout=10) as r:
            check(b'Keysight 34461A' in r.read(), 'HTML-Seite ist eingepackt')

        base_cfg = {'ip': '127.0.0.1', 'conn': 'socket', 'function': 'VOLT:DC', 'range': 'AUTO', 'nplc': '',
                    'unit': 'mV', 'extra': '', 'dut_count': '3', 'beep': 'device', 'tol': '1', 'stable_n': '5',
                    'reset': '50', 'idle': '20', 'poll': '0.15', 'interval': '0.2', 'trend_count': '0',
                    'manual_count': '3'}

        # Probe-Hold
        csv = tmp / 'probe.csv'
        r = api('/api/connect', dict(base_cfg, mode='probe', path=str(csv)))
        check(r.get('ok'), f'Verbinden Probe-Hold {r.get("err", "")}')
        api('/api/start', {})
        s = wait_for(lambda s: s['done'], 30, '3 DUTs erfasst')
        lines = csv.read_text(encoding='utf-8-sig').splitlines()
        check(lines[0] == 'Datum/Zeit;DUT;Messwert;Einheit' and len(lines) == 4, f'Probe-Hold CSV: {lines}')
        check(all(l.endswith(';mV') and ',' in l.split(';')[2] for l in lines[1:]), 'Dezimalkomma und Einheit')

        # Messwerte aufzeichnen
        csv = tmp / 'trend.csv'
        check(api('/api/connect', dict(base_cfg, mode='trend', path=str(csv)))['ok'], 'Verbinden Aufzeichnung')
        api('/api/start', {})
        time.sleep(3)
        api('/api/stop', {})
        s = api('/api/state?since=0&ver=-1')
        lines = csv.read_text(encoding='utf-8-sig').splitlines()
        check(s['count'] >= 8 and len(lines) == s['count'] + 1, f'Aufzeichnung: {s["count"]} Werte')

        # Einzelmessungen
        csv = tmp / 'manual.csv'
        check(api('/api/connect', dict(base_cfg, mode='manual', path=str(csv)))['ok'], 'Verbinden Einzelmessung')
        wait_for(lambda s: s['current'] is not None, 10, 'Live-Wert')
        api('/api/confirm', {})
        api('/api/confirm', {})
        api('/api/undo', {})
        lines = csv.read_text(encoding='utf-8-sig').splitlines()
        check(len(lines) == 3, f'Einzelmessung mit Ueberschreiben: {lines}')

        api('/api/quit', {})
        app.wait(timeout=20)
        check(True, 'Programm beendet sich ueber "Programm beenden"')
    finally:
        if app.poll() is None:
            app.kill()
        server.shutdown()


if __name__ == '__main__':
    main()
