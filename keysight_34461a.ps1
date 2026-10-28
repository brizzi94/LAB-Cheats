<#
Keysight 34461A - Messprogramm mit Browser-Oberflaeche (PowerShell-Version)
===========================================================================

Gleiche Funktion wie keysight_34461a_web.py, braucht aber keine Installation:
PowerShell ist in jedem Windows enthalten.

Start: Rechtsklick auf diese Datei -> "Mit PowerShell ausfuehren".
keysight_34461a.html muss im selben Ordner liegen. Der Browser oeffnet
sich mit dem Menue (Probe-Hold, Messwerte aufzeichnen, Einzelmessungen).

Das Skript laeuft als kleiner lokaler Server (nur localhost) und spricht per
LAN (Raw-Socket, Port 5025) mit dem Geraet. Messung und Speichern laufen im
Skript weiter, auch wenn der Browser-Tab im Hintergrund liegt oder geschlossen
wird. Beenden mit "Programm beenden" im Browser oder durch Schliessen des
Konsolenfensters.

CSV: Semikolon + Dezimalkomma, damit deutsches Excel die Datei direkt richtig
oeffnet. Eine vorhandene Datei wird weitergefuehrt.

Wichtig: Diese Datei muss als UTF-8 MIT BOM gespeichert bleiben, sonst zeigt
Windows PowerShell 5.1 die Einheiten (µ, Ω) falsch an.
#>

$ErrorActionPreference = 'Stop'

$Port = 8765
$PagePath = Join-Path $PSScriptRoot 'keysight_34461a.html'
$ConfigPath = Join-Path $HOME '.keysight_34461a_config.json'   # gleiche Datei wie die Python-Version
$Overload = 9.0e37          # Geraet meldet Ueberlast als +/-9.9E37
$MaxErrors = 3              # aufeinanderfolgende Lesefehler bis "Verbindung verloren"
$IdlePoll = 0.3             # s, Abfrage fuer die Live-Anzeige, wenn nichts aufgezeichnet wird
$LockMsg = 'Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen - Werte bleiben im Speicher und werden nachgeschrieben.'

$Inv = [Globalization.CultureInfo]::InvariantCulture
$Utf8NoBom = New-Object Text.UTF8Encoding $false
$Utf8Bom = New-Object Text.UTF8Encoding $true
$Clock = [Diagnostics.Stopwatch]::StartNew()

# Messfunktionen (identisch zur Python-Version): Anzeige, Messbereiche
# [SCPI-Wert, Text], Anzeigeeinheiten [Name, Faktor zum Geraetewert], NPLC einstellbar
$FunctionsJson = @'
{"VOLT:DC": {"label": "DC-Spannung", "nplc": true, "ranges": [["0.1", "100 mV"], ["1", "1 V"], ["10", "10 V"], ["100", "100 V"], ["1000", "1000 V"]], "units": [["mV", 1000.0], ["V", 1]]}, "VOLT:AC": {"label": "AC-Spannung", "nplc": false, "ranges": [["0.1", "100 mV"], ["1", "1 V"], ["10", "10 V"], ["100", "100 V"], ["750", "750 V"]], "units": [["mV", 1000.0], ["V", 1]]}, "CURR:DC": {"label": "DC-Strom", "nplc": true, "ranges": [["1E-4", "100 µA"], ["1E-3", "1 mA"], ["1E-2", "10 mA"], ["0.1", "100 mA"], ["1", "1 A"], ["3", "3 A"]], "units": [["mA", 1000.0], ["µA", 1000000.0], ["A", 1]]}, "CURR:AC": {"label": "AC-Strom", "nplc": false, "ranges": [["1E-4", "100 µA"], ["1E-3", "1 mA"], ["1E-2", "10 mA"], ["0.1", "100 mA"], ["1", "1 A"], ["3", "3 A"]], "units": [["mA", 1000.0], ["µA", 1000000.0], ["A", 1]]}, "RES": {"label": "Widerstand 2-Leiter", "nplc": true, "ranges": [["100", "100 Ω"], ["1E3", "1 kΩ"], ["1E4", "10 kΩ"], ["1E5", "100 kΩ"], ["1E6", "1 MΩ"], ["1E7", "10 MΩ"], ["1E8", "100 MΩ"]], "units": [["Ω", 1], ["kΩ", 0.001], ["MΩ", 1e-06]]}, "FRES": {"label": "Widerstand 4-Leiter", "nplc": true, "ranges": [["100", "100 Ω"], ["1E3", "1 kΩ"], ["1E4", "10 kΩ"], ["1E5", "100 kΩ"], ["1E6", "1 MΩ"], ["1E7", "10 MΩ"], ["1E8", "100 MΩ"]], "units": [["Ω", 1], ["mΩ", 1000.0], ["kΩ", 0.001]]}, "FREQ": {"label": "Frequenz", "nplc": false, "ranges": [], "units": [["Hz", 1], ["kHz", 0.001]]}, "CAP": {"label": "Kapazität", "nplc": false, "ranges": [["1E-9", "1 nF"], ["1E-8", "10 nF"], ["1E-7", "100 nF"], ["1E-6", "1 µF"], ["1E-5", "10 µF"], ["1E-4", "100 µF"]], "units": [["nF", 1000000000.0], ["µF", 1000000.0], ["pF", 1000000000000.0]]}}
'@
$Functions = $FunctionsJson | ConvertFrom-Json
$NplcValues = @('0.02', '0.2', '1', '10', '100')
$Headers = @{
    probe  = @('Datum/Zeit', 'DUT', 'Messwert', 'Einheit')
    trend  = @('Datum/Zeit', 'Zeit_s', 'Messwert', 'Einheit')
    manual = @('Datum/Zeit', 'Nr', 'Messwert', 'Einheit')
}

$script:S = $null
$script:Inst = $null
$script:Running = $true


# ---------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------
function Now-S { $Clock.Elapsed.TotalSeconds }

function Stamp { [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', $Inv) }

function CsvNum([double]$v, [string]$fmt = 'G7') { $v.ToString($fmt, $Inv).Replace('.', ',') }

function ErrMsg($err) {
    $ex = $err.Exception
    while ($ex.InnerException) { $ex = $ex.InnerException }
    $ex.Message
}

function JStr($s) {
    if ($null -eq $s) { return 'null' }
    $t = ([string]$s).Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n').Replace("`t", '\t')
    $t = [regex]::Replace($t, '[\x00-\x1f]', { param($m) '\u' + ([int][char]$m.Value).ToString('x4') })
    '"' + $t + '"'
}

function JNum($v) {
    if ($null -eq $v) { return 'null' }
    ([double]$v).ToString('R', $Inv)
}

function JBool($b) { if ($b) { 'true' } else { 'false' } }

function Get-Func($name) {
    $p = $Functions.PSObject.Properties[[string]$name]
    if ($p) { $p.Value } else { $null }
}

function Resolve-UserPath($text) {
    $t = ([string]$text).Trim()
    if ($t.StartsWith('~')) { $t = $HOME + $t.Substring(1) }
    if (-not [IO.Path]::IsPathRooted($t)) { $t = Join-Path $HOME $t }
    [IO.Path]::GetFullPath($t)
}

function Count-DataLines($path) {
    try {
        $n = 0
        foreach ($line in [IO.File]::ReadLines($path)) { if ($line.Trim()) { $n++ } }
        [math]::Max(0, $n - 1)
    } catch { 0 }
}

function Load-ConfigJson {
    try {
        $text = [IO.File]::ReadAllText($ConfigPath, $Utf8NoBom)
        $null = $text | ConvertFrom-Json
        if ($text.Trim().StartsWith('{')) { return $text }
    } catch { }
    '{}'
}


# ---------------------------------------------------------
# CSV-Datei: Zeilen werden angehaengt. Ist die Datei gesperrt (z.B. in
# Excel geoeffnet), bleiben sie in 'pending' und werden nachgeschrieben.
# ---------------------------------------------------------
function New-CsvLog($path, $header) {
    $log = @{ path = $path; pending = (New-Object Collections.Generic.List[string]); existing = 0 }
    if (-not (Test-Path -LiteralPath $path) -or (Get-Item -LiteralPath $path).Length -eq 0) {
        $dir = Split-Path -Parent $path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { $null = New-Item -ItemType Directory -Path $dir }
        # BOM, damit Excel Umlaute und Einheiten (µ, Ω) richtig anzeigt
        [IO.File]::WriteAllText($path, ($header -join ';') + "`r`n", $Utf8Bom)
    } else {
        $log.existing = Count-DataLines $path
        $fs = [IO.File]::Open($path, 'Append', 'Write')   # wirft, wenn die Datei gesperrt ist
        $fs.Close()
    }
    $log
}

function Log-Flush($log) {
    if ($log.pending.Count -eq 0) { return $true }
    try {
        [IO.File]::AppendAllText($log.path, ($log.pending -join ''), $Utf8NoBom)
        $log.pending.Clear()
        $true
    } catch { $false }
}

function Log-Append($log, $fields) {
    $log.pending.Add(($fields -join ';') + "`r`n")
    Log-Flush $log
}

function Log-DropLast($log) {
    # Entfernt die zuletzt angehaengte Zeile. False, wenn die Datei gesperrt ist.
    if ($log.pending.Count -gt 0) {
        $log.pending.RemoveAt($log.pending.Count - 1)
        return $true
    }
    try {
        $data = [IO.File]::ReadAllBytes($log.path)
        $end = $data.Length
        while ($end -gt 0 -and ($data[$end - 1] -eq 10 -or $data[$end - 1] -eq 13)) { $end-- }
        $cut = $end - 1
        while ($cut -ge 0 -and $data[$cut] -ne 10) { $cut-- }
        if ($cut -lt 0) { return $true }
        $fs = [IO.File]::Open($log.path, 'Open', 'Write')
        $fs.SetLength($cut + 1)
        $fs.Close()
        $true
    } catch { $false }
}


# ---------------------------------------------------------
# Geraet (SCPI ueber Raw-Socket, Port 5025)
# ---------------------------------------------------------
function Inst-Open($ip) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($ip, 5025, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(4000)) { throw 'keine Antwort (Zeitueberschreitung)' }
        $client.EndConnect($ar)
    } catch {
        $msg = ErrMsg $_
        $client.Close()
        throw "Geraet nicht erreichbar ($ip, Port 5025) - IP/LAN pruefen. ($msg)"
    }
    $client.NoDelay = $true
    $stream = $client.GetStream()
    $stream.ReadTimeout = 10000
    $stream.WriteTimeout = 5000
    $script:Inst = @{ client = $client; stream = $stream }
}

function Inst-Close {
    if ($script:Inst) {
        try { $script:Inst.client.Close() } catch { }
    }
    $script:Inst = $null
}

function Inst-Write([string]$cmd) {
    $bytes = [Text.Encoding]::ASCII.GetBytes($cmd + "`n")
    $script:Inst.stream.Write($bytes, 0, $bytes.Length)
}

function Inst-Query([string]$cmd) {
    Inst-Write $cmd
    $ms = New-Object IO.MemoryStream
    while ($true) {
        $b = $script:Inst.stream.ReadByte()
        if ($b -lt 0) { throw 'Verbindung vom Geraet getrennt.' }
        if ($b -eq 10) { break }
        $ms.WriteByte([byte]$b)
    }
    [Text.Encoding]::ASCII.GetString($ms.ToArray()).Trim()
}

function Inst-Drain {
    # Nach einer Zeitueberschreitung verspaetete Antworten verwerfen
    try {
        Start-Sleep -Milliseconds 300
        while ($script:Inst.stream.DataAvailable) { $null = $script:Inst.stream.ReadByte() }
    } catch { }
}

function Get-DeviceErrors {
    for ($i = 0; $i -lt 10; $i++) {
        $e = Inst-Query 'SYST:ERR?'
        if ($e.StartsWith('+0') -or $e.StartsWith('0')) { break }
        $e
    }
}

function Configure-Instrument($cfg) {
    # Stellt Messfunktion, Bereich und NPLC ein und liest zur Kontrolle zurueck
    $f = Get-Func $cfg.function
    Inst-Write '*CLS'
    if ($f.ranges.Count -gt 0) { Inst-Write ('CONF:' + $cfg.function + ' ' + $cfg.range) }
    else { Inst-Write ('CONF:' + $cfg.function) }
    if ($f.nplc -and $cfg.nplc) { Inst-Write ('SENS:' + $cfg.function + ':NPLC ' + $cfg.nplc) }
    foreach ($line in $cfg.extra) { Inst-Write $line }
    $errors = @(Get-DeviceErrors)
    if ($errors.Count -gt 0) { throw ('Geraet meldet Fehler: ' + ($errors -join ' | ')) }

    $f.label
    if ($f.ranges.Count -gt 0) {
        $label = 'Automatik'
        foreach ($r in $f.ranges) { if ($r[0] -eq $cfg.range) { $label = $r[1] } }
        'Bereich ' + $label
    }
    if ($f.nplc) {
        $n = [double]::Parse((Inst-Query ('SENS:' + $cfg.function + ':NPLC?')), $Inv)
        'NPLC ' + $n.ToString('G', $Inv).Replace('.', ',')
    }
    'CONF? ' + (Inst-Query 'CONF?').Trim().Trim('"')
}


# ---------------------------------------------------------
# Eingaben aus dem Menue pruefen
# ---------------------------------------------------------
function Cfg-Num($cfg, $key, [double]$lo, [double]$hi, [switch]$Int) {
    $v = 0.0
    $txt = ([string]$cfg[$key]).Trim().Replace(',', '.')
    if (-not [double]::TryParse($txt, [Globalization.NumberStyles]::Float, $Inv, [ref]$v)) { throw "Ungueltige Eingabe: $key" }
    if ($Int -and $v -ne [math]::Floor($v)) { throw "Ungueltige Eingabe: $key" }
    if ($v -lt $lo -or $v -gt $hi) { throw "$key muss zwischen $lo und $hi liegen." }
    $v
}

function Check-Config($data) {
    $cfg = @{}
    foreach ($p in $data.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    if (-not $Headers.ContainsKey([string]$cfg.mode)) { throw 'Unbekannter Modus.' }
    $cfg.ip = ([string]$cfg.ip).Trim()
    if (-not $cfg.ip) { throw 'Bitte IP-Adresse eingeben.' }
    $f = Get-Func $cfg.function
    if ($null -eq $f) { throw 'Unbekannte Messfunktion.' }
    $ranges = @(foreach ($r in $f.ranges) { $r[0] })
    if ($ranges -notcontains $cfg.range) { $cfg.range = 'AUTO' }
    if ($NplcValues -notcontains $cfg.nplc) { $cfg.nplc = '' }
    $factor = $null
    foreach ($u in $f.units) { if ($u[0] -ceq $cfg.unit) { $factor = [double]$u[1] } }
    if ($null -eq $factor) { $cfg.unit = $f.units[0][0]; $factor = [double]$f.units[0][1] }
    $cfg.factor = $factor
    $cfg.extra = @(([string]$cfg.extra) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($line in $cfg.extra) {
        if ($line.Contains('?')) { throw 'Zusaetzliche SCPI-Befehle duerfen keine Abfragen (?) enthalten.' }
    }
    if (-not ([string]$cfg.path).Trim()) { throw 'Bitte Speicherort fuer die CSV-Datei angeben.' }

    if ($cfg.mode -eq 'probe') {
        $cfg.target = Cfg-Num $cfg 'dut_count' 1 100000 -Int
        $cfg.tol = Cfg-Num $cfg 'tol' 1e-12 1e12
        $cfg.stable_n = Cfg-Num $cfg 'stable_n' 2 100 -Int
        $cfg.reset = Cfg-Num $cfg 'reset' 0 1e12
        $cfg.idle = Cfg-Num $cfg 'idle' 0 1e12
        $cfg.poll = Cfg-Num $cfg 'poll' 0.05 60
        if (@('pc', 'device', 'both', 'off') -notcontains $cfg.beep) { $cfg.beep = 'pc' }
    } elseif ($cfg.mode -eq 'trend') {
        $cfg.target = Cfg-Num $cfg 'trend_count' 0 10000000 -Int
        $cfg.interval = Cfg-Num $cfg 'interval' 0.1 86400
    } else {
        $cfg.target = Cfg-Num $cfg 'manual_count' 1 100000 -Int
    }
    $cfg
}


# ---------------------------------------------------------
# Messsitzung
# ---------------------------------------------------------
function Reset-State($cfg) {
    $ver = 1
    if ($script:S) { $ver = $script:S.rows_ver + 1 }
    $script:S = @{
        cfg = $cfg; mode = [string]$cfg.mode; connected = $false; lost = $false; info = @()
        running = $false; phase = ''           # Probe-Hold: 'stabil' | 'wechsel'
        window = (New-Object Collections.Generic.List[double]); ref = $null
        current = $null; overload = $false
        rows = (New-Object 'Collections.Generic.List[double[]]')   # [nr, wert, t_s]
        rows_ver = $ver; t0 = (Now-S); next_tick = 0.0; next_read = 0.0; errors = 0
        status = ''; status_kind = ''; event_seq = 0; event_kind = ''
        log = $null; path = ''; cmds = (New-Object Collections.Generic.Queue[string])
    }
}

function Set-Status($text, $kind = '') {
    $script:S.status = $text
    $script:S.status_kind = $kind
}

function Fire-Event($kind) {
    $s = $script:S
    $s.event_seq++
    $s.event_kind = $kind
    if ($s.mode -eq 'probe' -and @('device', 'both') -contains $s.cfg.beep) {
        $s.cmds.Enqueue('SYST:BEEP')
        if ($kind -eq 'done') { $s.cmds.Enqueue('SYST:BEEP') }
    }
}

function Is-Done {
    $t = $script:S.cfg.target
    ($t -gt 0) -and ($script:S.rows.Count -ge $t)
}

function Fields($nr, $v, $t, $ts) {
    if ($script:S.mode -eq 'trend') { $second = CsvNum $t 'F3' } else { $second = [string][int]$nr }
    $ts
    $second
    CsvNum $v
    $script:S.cfg.unit
}

function Add-Row($nr, $v, $t) {
    $script:S.rows.Add([double[]]@($nr, $v, [math]::Round($t, 3)))
}

function Record($v, $t) {
    $s = $script:S
    $nr = $s.rows.Count + 1
    $ok = Log-Append $s.log @(Fields $nr $v $t (Stamp))
    Add-Row $nr $v $t
    if (Is-Done) {
        $s.running = $false
        $s.phase = ''
        Fire-Event 'done'
        if ($ok) { Set-Status 'Alle Messungen erfasst.' 'ok' } else { Set-Status $LockMsg 'err' }
    } else {
        if ($s.mode -eq 'probe') { Fire-Event 'ok' }
        if ($ok) { Set-Status 'Gespeichert.' 'ok' } else { Set-Status $LockMsg 'err' }
    }
}

function Win-Spread {
    $w = $script:S.window
    if ($w.Count -lt 2) { return $null }
    $lo = $w[0]; $hi = $w[0]
    foreach ($x in $w) { if ($x -lt $lo) { $lo = $x }; if ($x -gt $hi) { $hi = $x } }
    $hi - $lo
}

function Probe-Step($v) {
    $s = $script:S
    $c = $s.cfg
    $contact = ($null -ne $v) -and ($c.idle -le 0 -or [math]::Abs($v) -ge $c.idle)
    if ($s.phase -eq 'stabil') {
        if (-not $contact) { $s.window.Clear(); return }
        $s.window.Add($v)
        while ($s.window.Count -gt $c.stable_n) { $s.window.RemoveAt(0) }
        if ($s.window.Count -eq $c.stable_n -and (Win-Spread) -lt $c.tol) {
            $sum = 0.0
            foreach ($x in $s.window) { $sum += $x }
            $value = $sum / $s.window.Count
            $s.window.Clear()
            $s.ref = $value
            Record $value ((Now-S) - $s.t0)
            if ($s.running) { $s.phase = 'wechsel' }
        }
    } elseif ($s.phase -eq 'wechsel') {
        if (-not $contact -or [math]::Abs($v - $s.ref) -gt $c.reset) {
            $s.phase = 'stabil'
            $s.window.Clear()
        }
    }
}

function Measure-Step {
    # Wird in der Hauptschleife laufend aufgerufen; misst nur, wenn es Zeit ist
    $s = $script:S
    if (-not $s -or -not $s.connected) { return }
    if ($s.log.pending.Count -gt 0) {
        if ((Log-Flush $s.log) -and $s.status -eq $LockMsg) { Set-Status 'Datei wieder frei - Werte nachgeschrieben.' 'ok' }
    }
    try { while ($s.cmds.Count -gt 0) { Inst-Write ($s.cmds.Dequeue()) } } catch { }

    $now = Now-S
    if ($s.mode -eq 'trend' -and $s.running) {
        if ($now -lt $s.next_tick) { return }
    } else {
        if ($now -lt $s.next_read) { return }
        if ($s.mode -eq 'probe') { $s.next_read = $now + $s.cfg.poll } else { $s.next_read = $now + $IdlePoll }
    }

    try {
        $raw = [double]::Parse((Inst-Query 'READ?'), $Inv)
        $s.errors = 0
    } catch {
        $s.errors++
        $msg = ErrMsg $_
        if ($s.errors -ge $MaxErrors) {
            $s.connected = $false
            $s.lost = $true
            $s.running = $false
            $s.current = $null
            Fire-Event 'err'
            Set-Status "Verbindung zum Geraet verloren ($msg)." 'err'
            Inst-Close
        } else {
            Set-Status "Lesefehler: $msg" 'err'
            Inst-Drain
        }
        return
    }

    $now = Now-S
    if ([math]::Abs($raw) -ge $Overload) { $v = $null } else { $v = $raw * $s.cfg.factor }
    $s.current = $v
    $s.overload = ($null -eq $v)
    if ($s.mode -eq 'trend' -and $s.running) {
        $s.next_tick = [math]::Max($s.next_tick + $s.cfg.interval, $now)
        if ($null -eq $v) { Set-Status 'Ueberlast - Wert nicht gespeichert.' 'err' }
        else { Record $v ($now - $s.t0) }
    } elseif ($s.mode -eq 'probe' -and $s.running) {
        Probe-Step $v
    }
}


# ---------------------------------------------------------
# Aktionen aus der Oberflaeche (liefern JSON-Text)
# ---------------------------------------------------------
function Do-Connect($data, $body) {
    $cfg = Check-Config $data
    $path = Resolve-UserPath $cfg.path
    $null = Do-Disconnect
    Inst-Open $cfg.ip
    try {
        $idn = Inst-Query '*IDN?'
        if (-not $idn) { throw 'Geraet antwortet nicht auf *IDN?' }
        $info = @(Configure-Instrument $cfg)
        try { $log = New-CsvLog $path $Headers[$cfg.mode] }
        catch { throw "Datei $path kann nicht geschrieben werden (z.B. in Excel geoeffnet?). $(ErrMsg $_)" }
    } catch {
        Inst-Close
        throw
    }
    try { [IO.File]::WriteAllText($ConfigPath, $body, $Utf8NoBom) } catch { }
    Reset-State $cfg
    $s = $script:S
    $s.log = $log
    $s.connected = $true
    $s.info = $info
    $s.path = $path
    if ($log.existing -gt 0) { Set-Status "$($log.existing) vorhandene Zeilen in der Datei, neue werden angehaengt." 'ok' }
    else { Set-Status 'Neue Datei angelegt.' 'ok' }
    '{"ok":true}'
}

function Do-Disconnect {
    $s = $script:S
    if ($s) {
        if ($s.log) { $null = Log-Flush $s.log }
        $s.connected = $false
        $s.running = $false
    }
    Inst-Close
    '{"ok":true}'
}

function Do-Start {
    $s = $script:S
    if (-not $s -or -not $s.connected -or $s.mode -eq 'manual' -or $s.running -or (Is-Done)) { return '{"ok":false}' }
    $now = Now-S
    $last = $null
    if ($s.rows.Count -gt 0) { $last = $s.rows[$s.rows.Count - 1] }
    if ($s.mode -eq 'trend') {
        # Fortsetzen: Zeitachse laeuft ab dem letzten Wert weiter
        if ($last) { $s.t0 = $now - ($last[2] + $s.cfg.interval) } else { $s.t0 = $now }
        $s.next_tick = $now
        Set-Status 'Aufzeichnung laeuft ...' 'ok'
    } else {
        # Nach einer Pause zuerst den Kontaktwechsel abwarten,
        # damit das letzte DUT nicht doppelt erfasst wird
        if ($last) { $s.phase = 'wechsel'; $s.ref = $last[1] } else { $s.phase = 'stabil'; $s.ref = $null }
        $s.window.Clear()
        Set-Status 'Messung laeuft - Pruefspitzen ansetzen.' 'ok'
    }
    $s.running = $true
    '{"ok":true}'
}

function Do-Stop {
    $s = $script:S
    if (-not $s) { return '{"ok":false}' }
    $s.running = $false
    $s.phase = ''
    $ok = $true
    if ($s.log) { $ok = Log-Flush $s.log }
    if ($ok) { Set-Status 'Gestoppt - Datei gespeichert.' 'ok' } else { Set-Status $LockMsg 'err' }
    '{"ok":true}'
}

function Do-Confirm {
    $s = $script:S
    if (-not $s -or -not $s.connected -or $s.mode -ne 'manual' -or (Is-Done)) { return '{"ok":false}' }
    if ($null -eq $s.current) { Set-Status 'Kein gueltiger Messwert (Ueberlast?).' 'err'; return '{"ok":false}' }
    $nr = $s.rows.Count + 1
    $t = (Now-S) - $s.t0
    if (-not (Log-Append $s.log @(Fields $nr $s.current $t (Stamp)))) {
        $s.log.pending.RemoveAt($s.log.pending.Count - 1)
        Set-Status 'Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen und erneut versuchen.' 'err'
        return '{"ok":false}'
    }
    Add-Row $nr $s.current $t
    if (Is-Done) { Set-Status 'Alle Messungen erfasst.' 'ok' } else { Set-Status 'Gespeichert.' 'ok' }
    '{"ok":true}'
}

function Do-Undo {
    # Einzelmessung: letzten Wert durch den aktuellen ersetzen.
    # Probe-Hold: letztes DUT verwerfen und neu messen.
    $s = $script:S
    if (-not $s -or -not $s.connected -or (@('probe', 'manual') -notcontains $s.mode) -or $s.rows.Count -eq 0) { return '{"ok":false}' }
    if ($s.mode -eq 'manual' -and $null -eq $s.current) { Set-Status 'Kein gueltiger Messwert (Ueberlast?).' 'err'; return '{"ok":false}' }
    if (-not (Log-DropLast $s.log)) {
        Set-Status 'Datei gesperrt (z.B. in Excel geoeffnet). Bitte schliessen und erneut versuchen.' 'err'
        return '{"ok":false}'
    }
    $dropped = $s.rows[$s.rows.Count - 1]
    $s.rows.RemoveAt($s.rows.Count - 1)
    $s.rows_ver++
    if ($s.mode -eq 'manual') {
        $t = (Now-S) - $s.t0
        $ok = Log-Append $s.log @(Fields $dropped[0] $s.current $t (Stamp))
        Add-Row $dropped[0] $s.current $t
        if ($ok) { Set-Status 'Letzter Wert ueberschrieben.' 'ok' } else { Set-Status $LockMsg 'err' }
    } else {
        # Pruefspitzen loesen und neu ansetzen, dann wird das DUT neu erfasst
        $s.ref = $dropped[1]
        $s.window.Clear()
        $s.phase = 'wechsel'
        $s.running = $true
        Set-Status "DUT $([int]$dropped[0]) verworfen - Pruefspitzen neu ansetzen." 'ok'
    }
    '{"ok":true}'
}

function Get-StateJson($since, $ver) {
    $s = $script:S
    if (-not $s) { Reset-State @{}; $s = $script:S }
    if ($ver -ne $s.rows_ver -or $since -gt $s.rows.Count -or $since -lt 0) { $since = 0 }
    $c = $s.cfg
    $rows = New-Object Text.StringBuilder
    for ($i = $since; $i -lt $s.rows.Count; $i++) {
        $r = $s.rows[$i]
        if ($i -gt $since) { $null = $rows.Append(',') }
        $null = $rows.Append('[').Append($r[0].ToString('R', $Inv)).Append(',').Append($r[1].ToString('R', $Inv)).Append(',').Append($r[2].ToString('R', $Inv)).Append(']')
    }
    $done = $false
    if ($s.connected -or $s.lost) { $done = Is-Done }
    $target = 0
    if ($c.target) { $target = $c.target }
    $saveOk = -not ($s.log -and $s.log.pending.Count -gt 0)
    $info = (@($s.info) | ForEach-Object { JStr $_ }) -join ','
    # Jedes Element auf eigener Zeile: in PowerShell bindet das Komma staerker als '+'
    $parts = @(
        '"connected":' + (JBool $s.connected)
        '"lost":' + (JBool $s.lost)
        '"mode":' + (JStr $s.mode)
        '"running":' + (JBool $s.running)
        '"done":' + (JBool $done)
        '"phase":' + (JStr $s.phase)
        '"fill":' + $s.window.Count
        '"spread":' + (JNum (Win-Spread))
        '"stable_n":' + (JNum $c.stable_n)
        '"tol":' + (JNum $c.tol)
        '"interval":' + (JNum $c.interval)
        '"target":' + (JNum $target)
        '"unit":' + (JStr ([string]$c.unit))
        '"beep_pc":' + (JBool (@('pc', 'both') -contains $c.beep))
        '"current":' + (JNum $s.current)
        '"overload":' + (JBool $s.overload)
        '"count":' + $s.rows.Count
        '"rows":[' + $rows.ToString() + ']'
        '"rows_from":' + $since
        '"rows_ver":' + $s.rows_ver
        '"status":' + (JStr $s.status)
        '"status_kind":' + (JStr $s.status_kind)
        '"event_seq":' + $s.event_seq
        '"event_kind":' + (JStr $s.event_kind)
        '"info":[' + $info + ']'
        '"path":' + (JStr $s.path)
        '"save_ok":' + (JBool $saveOk)
    )
    '{' + ($parts -join ',') + '}'
}

function Do-Browse($data) {
    # Speichern-Dialog von Windows fuer die CSV-Datei
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $cur = ([string]$data.path).Trim()
        if ($cur) { $curPath = Resolve-UserPath $cur } else { $curPath = Join-Path $HOME 'messungen.csv' }
        $dlg = New-Object Windows.Forms.SaveFileDialog
        $dlg.Title = 'CSV-Datei waehlen/erstellen'
        $dlg.Filter = 'CSV-Datei (Excel)|*.csv'
        $dlg.DefaultExt = 'csv'
        $dlg.AddExtension = $true
        $dlg.OverwritePrompt = $false
        $dir = Split-Path -Parent $curPath
        if ($dir -and (Test-Path -LiteralPath $dir)) { $dlg.InitialDirectory = $dir }
        $dlg.FileName = Split-Path -Leaf $curPath
        $owner = New-Object Windows.Forms.Form
        $owner.TopMost = $true
        $result = $dlg.ShowDialog($owner)
        $owner.Dispose()
        if ($result -ne [Windows.Forms.DialogResult]::OK) { return '{"ok":true,"path":""}' }
        $p = $dlg.FileName
        $existing = -1
        if (Test-Path -LiteralPath $p) { $existing = Count-DataLines $p }
        '{"ok":true,"path":' + (JStr $p) + ',"existing":' + $existing + '}'
    } catch {
        '{"ok":false,"err":' + (JStr ("Dialog nicht verfuegbar ($(ErrMsg $_)) - Pfad bitte eintippen.")) + '}'
    }
}

function Do-FileInfo($data) {
    $p = Resolve-UserPath ([string]$data.path)
    $existing = -1
    if (Test-Path -LiteralPath $p) { $existing = Count-DataLines $p }
    '{"ok":true,"path":' + (JStr $p) + ',"existing":' + $existing + '}'
}

function Get-MetaJson {
    '{"functions":' + $FunctionsJson + ',"nplc":["' + ($NplcValues -join '","') + '"],"vxi11":false,"config":' + (Load-ConfigJson) + '}'
}


# ---------------------------------------------------------
# Lokaler Webserver (TcpListener nur auf 127.0.0.1: braucht keine
# Adminrechte und loest keine Firewall-Abfrage aus)
# ---------------------------------------------------------
function Route-Request($method, $path, $query, $headers, $body) {
    # Liefert @{ code; ctype; text } bzw. @{ code; ctype; bytes }
    $json = 'application/json; charset=utf-8'
    # Schutz gegen fremde Webseiten (DNS-Rebinding)
    if (@("localhost:$Port", "127.0.0.1:$Port") -notcontains $headers['host']) {
        return @{ code = 403; ctype = $json; text = '{"ok":false}' }
    }
    if ($method -eq 'GET') {
        if ($path -eq '/') { return @{ code = 200; ctype = 'text/html; charset=utf-8'; bytes = [IO.File]::ReadAllBytes($PagePath) } }
        if ($path -eq '/api/meta') { return @{ code = 200; ctype = $json; text = (Get-MetaJson) } }
        if ($path -eq '/api/state') {
            $since = 0; $ver = -1
            if ($query -match '(^|&)since=(-?\d+)') { $since = [int]$Matches[2] }
            if ($query -match '(^|&)ver=(-?\d+)') { $ver = [int]$Matches[2] }
            return @{ code = 200; ctype = $json; text = (Get-StateJson $since $ver) }
        }
        return @{ code = 404; ctype = $json; text = '{"ok":false}' }
    }
    # Nur JSON von der eigenen Seite annehmen (fremde Seiten koennen
    # diesen Content-Type nicht ohne Rueckfrage senden)
    if ($method -ne 'POST' -or -not ([string]$headers['content-type']).StartsWith('application/json')) {
        return @{ code = 403; ctype = $json; text = '{"ok":false}' }
    }
    try {
        if ($body.Trim()) { $data = $body | ConvertFrom-Json } else { $data = New-Object PSObject }
        switch ($path) {
            '/api/connect'    { $out = Do-Connect $data $body }
            '/api/disconnect' { $out = Do-Disconnect }
            '/api/start'      { $out = Do-Start }
            '/api/stop'       { $out = Do-Stop }
            '/api/confirm'    { $out = Do-Confirm }
            '/api/undo'       { $out = Do-Undo }
            '/api/browse'     { $out = Do-Browse $data }
            '/api/fileinfo'   { $out = Do-FileInfo $data }
            '/api/quit'       { $null = Do-Disconnect; $script:Running = $false; $out = '{"ok":true}' }
            default           { return @{ code = 404; ctype = $json; text = '{"ok":false}' } }
        }
        @{ code = 200; ctype = $json; text = [string]$out }
    } catch {
        @{ code = 200; ctype = $json; text = ('{"ok":false,"err":' + (JStr (ErrMsg $_)) + '}') }
    }
}

function Serve-Client($client) {
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $buf = New-Object IO.MemoryStream
        $tmp = New-Object byte[] 8192
        $headerEnd = -1
        while ($headerEnd -lt 0) {
            $n = $stream.Read($tmp, 0, $tmp.Length)
            if ($n -le 0) { return }
            $buf.Write($tmp, 0, $n)
            if ($buf.Length -gt 1MB) { return }
            # ASCII-Dekodierung: ein Zeichen pro Byte, Positionen bleiben gleich
            $headerEnd = [Text.Encoding]::ASCII.GetString($buf.ToArray()).IndexOf("`r`n`r`n")
        }
        $all = $buf.ToArray()
        $lines = [Text.Encoding]::ASCII.GetString($all, 0, $headerEnd) -split "`r`n"
        $first = $lines[0] -split ' '
        $headers = @{}
        foreach ($line in $lines[1..($lines.Count - 1)]) {
            $i = $line.IndexOf(':')
            if ($i -gt 0) { $headers[$line.Substring(0, $i).Trim().ToLower()] = $line.Substring($i + 1).Trim() }
        }
        $len = 0
        if ($headers['content-length']) { $len = [int]$headers['content-length'] }
        $bodyStart = $headerEnd + 4
        while ($buf.Length - $bodyStart -lt $len) {
            $n = $stream.Read($tmp, 0, $tmp.Length)
            if ($n -le 0) { return }
            $buf.Write($tmp, 0, $n)
        }
        $all = $buf.ToArray()
        $body = $Utf8NoBom.GetString($all, $bodyStart, $len)

        $target = [string]$first[1]
        $q = $target.IndexOf('?')
        if ($q -ge 0) { $path = $target.Substring(0, $q); $query = $target.Substring($q + 1) }
        else { $path = $target; $query = '' }

        $res = Route-Request $first[0] $path $query $headers $body
        if ($null -ne $res.bytes) { $payload = $res.bytes } else { $payload = $Utf8NoBom.GetBytes($res.text) }
        $reason = @{ 200 = 'OK'; 403 = 'Forbidden'; 404 = 'Not Found' }[$res.code]
        $head = "HTTP/1.1 $($res.code) $reason`r`nContent-Type: $($res.ctype)`r`nContent-Length: $($payload.Length)`r`nCache-Control: no-store`r`nConnection: close`r`n`r`n"
        $hb = [Text.Encoding]::ASCII.GetBytes($head)
        $stream.Write($hb, 0, $hb.Length)
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush()
    } catch {
        # Browser hat die Verbindung abgebrochen o.ae. - ignorieren
    } finally {
        $client.Close()
    }
}


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------
try { $Host.UI.RawUI.WindowTitle = 'Keysight 34461A - Messprogramm' } catch { }
if (-not (Test-Path -LiteralPath $PagePath)) {
    Write-Host 'keysight_34461a.html fehlt - muss im selben Ordner wie dieses Skript liegen.' -ForegroundColor Red
    Read-Host 'Enter zum Beenden'
    exit 1
}

$listener = $null
foreach ($p in 8765..8775) {
    $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $p)
    try {
        $l.Start()
        $listener = $l
        $Port = $p
        break
    } catch {
        if ($p -eq 8765) {
            # Laeuft das Programm schon? Dann nur den Browser oeffnen.
            try {
                $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$p/api/meta" -TimeoutSec 2
                if ($r.Content -like '*"functions"*') {
                    Write-Host 'Das Messprogramm laeuft bereits - oeffne den Browser.'
                    Start-Process "http://127.0.0.1:$p/"
                    Start-Sleep -Seconds 2
                    exit 0
                }
            } catch { }
        }
    }
}
if (-not $listener) {
    Write-Host 'Kein freier Port gefunden (8765-8775).' -ForegroundColor Red
    Read-Host 'Enter zum Beenden'
    exit 1
}

$url = "http://127.0.0.1:$Port/"
Write-Host 'Keysight 34461A - Messprogramm'
Write-Host "Oberflaeche: $url"
Write-Host 'Dieses Fenster offen lassen. Beenden mit "Programm beenden" im Browser oder Fenster schliessen.'
if (-not $env:KEYSIGHT_NO_BROWSER) { Start-Process $url }

# Offene Verbindungen, die noch keine Anfrage geschickt haben (Browser
# oeffnen oft vorsorglich Verbindungen) - nicht darauf warten, sonst stockt die Messung
$conns = New-Object Collections.Generic.List[object]
try {
    while ($script:Running) {
        while ($listener.Pending()) { $conns.Add(@{ client = $listener.AcceptTcpClient(); since = (Now-S) }) }
        for ($i = $conns.Count - 1; $i -ge 0; $i--) {
            $k = $conns[$i]
            if ($k.client.Available -gt 0) {
                $conns.RemoveAt($i)
                Serve-Client $k.client
            } elseif ((Now-S) - $k.since -gt 30) {
                $conns.RemoveAt($i)
                $k.client.Close()
            }
        }
        try { Measure-Step } catch { if ($script:S) { Set-Status "Fehler: $(ErrMsg $_)" 'err' } }
        Start-Sleep -Milliseconds 10
    }
} finally {
    $null = Do-Disconnect
    foreach ($k in $conns) { $k.client.Close() }
    $listener.Stop()
    Write-Host 'Beendet.'
}
