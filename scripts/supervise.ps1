<#
.SYNOPSIS
  Супервизор OSKAR 24/7: держит живыми демон oskard и шлюз Hermes (Telegram).

.DESCRIPTION
  Зачем: oskard и hermes gateway запускались из сессии Claude и умирали вместе с ней
  (15-16.10: 19 ч простоя, ни одного сообщения в Telegram). Супервизор живет отдельно
  (Task Scheduler, scripts\install_autostart.ps1) и:

  - стартует oskard (uv run oskard) и hermes gateway с нужным окружением, вывод — в
    data\oskard.stdout.log и data\hermes_gateway_sandbox.log (дозапись);
  - каждые IntervalSec проверяет, живы ли процессы; oskard — еще GET /status на
    127.0.0.1:8321 с X-Oskar-Token из data\oskard.token (таймаут 5 с, без прокси);
  - упал -> рестарт с экспоненциальной задержкой 10 с -> 5 мин; 30 мин стабильной
    работы сбрасывают задержку;
  - НЕ стартует второй oskard: любой процесс oskard на машине, слушающий порт API или
    занятый data\oskard.lock = экземпляр уже есть (урок v1: два экземпляра = двойная
    позиция). Чужой экземпляр супервизор только наблюдает;
  - алерт в Telegram (сигнальный бот) при падении и восстановлении, не чаще 1 в 10 мин
    на процесс (более частые события сворачиваются в одно сообщение);
  - сторож зависания: строка «heartbeat» потока сканера (раз в 5 мин) в data\oskard.log
    старше HangMinutes -> алерт «демон завис». Без авто-kill: убивать торгового демона
    посреди выставления ордера опаснее, чем позвать Ивана;
  - держит систему бодрствующей (SetThreadExecutionState ES_CONTINUOUS|ES_SYSTEM_REQUIRED),
    пока работает: сон ПК замораживал демон (13-14.10). Настройки питания не трогаются,
    экран гаснуть может;
  - лог data\supervisor.log (UTF-8 с BOM, ротация по размеру). Токены не пишутся.

  Остановка супервизора НЕ останавливает oskard и hermes (они живут в своих скрытых
  консолях); следующий запуск супервизора их подхватит.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\supervise.ps1 -DryRun
  План без запуска чего-либо и без сети.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\supervise.ps1 -Once
  Одна итерация: поднять то, что лежит, и выйти (процессы продолжают работать).
#>
[CmdletBinding()]
param(
    [switch]$Once,                    # одна итерация и выход (для теста)
    [switch]$DryRun,                  # ничего не запускать, не ходить в сеть: напечатать план
    [string]$RepoRoot = '',            # '' -> родитель каталога scripts
    [string]$ApiHost = '',            # '' -> config\settings.yaml api.host -> 127.0.0.1
    [int]$ApiPort = 0,                # 0  -> config\settings.yaml api.port -> 8321
    [string]$UvExe = '',              # '' -> %USERPROFILE%\.local\bin\uv.exe -> uv из PATH
    [string]$OskardCommand = '',      # '' -> "<uv.exe>" run oskard
    [string]$HermesHome = '',         # '' -> %HERMES_HOME% -> %USERPROFILE%\hermes
    [string]$HermesCommand = '',      # '' -> "<HermesHome>\bin\hermes.exe" gateway run
    [string]$HermesLog = '',          # '' -> <repo>\data\hermes_gateway_sandbox.log
    [switch]$NoHermes,                # не надзирать за шлюзом Hermes
    [switch]$NoTelegram,              # алерты только в лог
    [string]$TelegramProxy = '',      # '' -> HTTPS_PROXY/ALL_PROXY/HTTP_PROXY -> системный -> напрямую
    [int]$IntervalSec = 10,
    [int]$HealthEverySec = 30,
    [int]$HealthTimeoutSec = 5,
    [int]$HealthFailThreshold = 3,
    [int]$StartupGraceSec = 180,      # столько после старта неответ API не считается сбоем
    [int]$HangMinutes = 15,           # порог сторожа: heartbeat старше -> «демон завис»
    [int]$BackoffBaseSec = 10,
    [int]$BackoffMaxSec = 300,
    [int]$StableResetMinutes = 30,
    [int]$AlertCooldownMinutes = 10,
    [int]$HermesStableSec = 60,       # столько шлюз должен прожить, чтобы считаться восстановленным
    [int]$KeepAwakeMinutes = 5,
    [int]$MaxLogMB = 5,
    [int]$MaxIterations = 0,          # 0 = бесконечно; для тестов
    [string]$OskardProcessPattern = '(?i)(\\oskard(\.exe)?"?\s*$|\buv(\.exe)?"?\s+run\s+oskard\s*$|-m\s+oskar\.core\.daemon\b)',
    [string]$HermesProcessPattern = '(?i)hermes(\.exe)?"?\s+gateway(\s+run)?(\s+-\S+)*\s*$'
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
if ($DryRun) { $Once = $true }

# ============================================================== пути и константы
# $PSScriptRoot в значениях по умолчанию param() у PS 5.1 пуст (-File) — вычисляем здесь
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $ScriptDir }
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$DataDir = Join-Path $RepoRoot 'data'
$SupLog = Join-Path $DataDir 'supervisor.log'
$OskardStdout = Join-Path $DataDir 'oskard.stdout.log'
$OskardLog = Join-Path $DataDir 'oskard.log'
$TokenFile = Join-Path $DataDir 'oskard.token'
$LockFile = Join-Path $DataDir 'oskard.lock'
$DotEnvPath = Join-Path $RepoRoot '.env'
$TradingHostFile = Join-Path $ScriptDir 'trading_host.txt'
$MutexName = 'Global\OSKAR_Supervisor'
$MachineName = $env:COMPUTERNAME
# Контракт с src/oskar/core/shadow_runner.py (HEARTBEAT_SEC, строка «heartbeat: tick N, ...»)
# и форматом logging_setup.LOG_FORMAT; проверяется тестом tests/unit/test_supervisor_scripts.py.
$HeartbeatPattern = '(?m)^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} INFO oskar\.shadow: heartbeat: tick \d+'
$HeartbeatTailBytes = 262144
$ChildLogMaxBytes = 20MB
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
$script:Secrets = New-Object System.Collections.Generic.List[string]
$script:Alerts = @{}
$script:ProcSnap = @()
$script:ProcSnapOk = $false
$script:DotEnv = $null
$script:DotEnvStamp = $null
$script:HostRefusalSent = $false
$script:TelegramWarned = $false

# ================================================================== лог и секреты
function Add-Secret([string]$Value) {
    if ($Value -and $Value.Length -ge 6 -and -not $script:Secrets.Contains($Value)) { $script:Secrets.Add($Value) }
}

function Protect-Text([string]$Text) {
    if (-not $Text) { return $Text }
    foreach ($s in $script:Secrets) { $Text = $Text.Replace($s, '<redacted>') }
    # токен бота (bot<id>:<secret>) и логин:пароль в URL прокси
    $Text = [regex]::Replace($Text, '\d{6,}:[A-Za-z0-9_-]{20,}', '<redacted>')
    $Text = [regex]::Replace($Text, '(?i)(\w+://)[^/\s:@]+:[^/\s@]+@', '$1<redacted>@')
    return $Text
}

function Invoke-LogRotation([string]$Path, [long]$MaxBytes, [int]$Keep) {
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        if ((Get-Item -LiteralPath $Path).Length -lt $MaxBytes) { return }
        for ($i = $Keep - 1; $i -ge 1; $i--) {
            $src = "$Path.$i"
            if (Test-Path -LiteralPath $src) { Move-Item -LiteralPath $src -Destination "$Path.$($i + 1)" -Force -ErrorAction Stop }
        }
        Move-Item -LiteralPath $Path -Destination "$Path.1" -Force -ErrorAction Stop
    } catch {
        # файл держит чужой процесс (редирект живого экземпляра) — ротируем в другой раз
        Write-Host "rotation skipped for ${Path}: $($_.Exception.Message)"
    }
}

function Write-Log([string]$Level, [string]$Message) {
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, (Protect-Text $Message)
    Write-Host $line
    if ($DryRun) { return }
    try {
        if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
        Invoke-LogRotation $SupLog ([long]$MaxLogMB * 1MB) 3
        # BOM пишется только при создании файла: Get-Content в PS 5.1 читает без -Encoding
        [System.IO.File]::AppendAllText($SupLog, $line + "`r`n", $Utf8Bom)
    } catch {
        Write-Host "supervisor.log write failed: $($_.Exception.Message)"
    }
}

function Add-Marker([string]$Path, [string]$Text) {
    try { [System.IO.File]::AppendAllText($Path, $Text + "`r`n", $Utf8) } catch { }
}

function Get-UtcStamp { return (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

# ================================================================ .env и настройки
function Read-DotEnv([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in [System.IO.File]::ReadAllLines($Path, $Utf8)) {
        if ($line -notmatch '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $key = $Matches[1]
        $val = $Matches[2].Trim()
        if ($val.Length -ge 1 -and ($val[0] -eq '"' -or $val[0] -eq "'")) {
            $end = $val.IndexOf($val[0], 1)
            if ($end -gt 0) { $val = $val.Substring(1, $end - 1) } else { $val = $val.Substring(1) }
        } else {
            $val = ($val -replace '\s+#.*$', '').Trim()
        }
        $map[$key] = $val
    }
    return $map
}

function Get-DotEnv {
    $stamp = $null
    if (Test-Path -LiteralPath $DotEnvPath) { $stamp = (Get-Item -LiteralPath $DotEnvPath).LastWriteTimeUtc }
    if ($null -eq $script:DotEnv -or $stamp -ne $script:DotEnvStamp) {
        $script:DotEnv = Read-DotEnv $DotEnvPath
        $script:DotEnvStamp = $stamp
    }
    return $script:DotEnv
}

function Get-Setting([string]$Name) {
    # как python-dotenv у демона: переменная окружения главнее .env
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($v) { return $v.Trim() }
    $map = Get-DotEnv
    if ($map.ContainsKey($Name)) { return [string]$map[$Name] }
    return ''
}

function Get-ApiFromSettings {
    $result = @{ Host = '127.0.0.1'; Port = 8321 }
    $f = Join-Path $RepoRoot 'config\settings.yaml'
    if (-not (Test-Path -LiteralPath $f)) { return $result }
    try {
        $text = [System.IO.File]::ReadAllText($f, $Utf8)
        $m = [regex]::Match($text, '(?m)^api:[ \t]*(?:#.*)?\r?\n((?:[ \t]+.*(?:\r?\n|$)|[ \t]*\r?\n)*)')
        if ($m.Success) {
            $block = $m.Groups[1].Value
            $mh = [regex]::Match($block, '(?m)^[ \t]+host:[ \t]*["'']?([^"''\s#]+)')
            if ($mh.Success) { $result.Host = $mh.Groups[1].Value }
            $mp = [regex]::Match($block, '(?m)^[ \t]+port:[ \t]*(\d+)')
            if ($mp.Success) { $result.Port = [int]$mp.Groups[1].Value }
        }
    } catch { }
    return $result
}

function Get-TradingHost {
    if (-not (Test-Path -LiteralPath $TradingHostFile)) { return '' }
    foreach ($line in [System.IO.File]::ReadAllLines($TradingHostFile, $Utf8)) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#')) { return $t }
    }
    return ''
}

function Test-TradingHost {
    # Демон — только на ОДНОЙ машине (port-lock локальный; две машины = двойные позиции).
    $th = Get-TradingHost
    if (-not $th -or $th -eq $MachineName) { return $true }
    if (-not $script:HostRefusalSent) {
        $script:HostRefusalSent = $true
        Send-Alert 'supervisor' ("Супервизор на {0} НЕ запускает oskard/hermes: торговый ПК по scripts\trading_host.txt — {1}. Демон работает только на одной машине." -f $MachineName, $th)
    }
    return $false
}

# ============================================================ разрешение конфигурации
$api = Get-ApiFromSettings
if (-not $ApiHost) { $ApiHost = $api.Host }
if ($ApiPort -le 0) { $ApiPort = $api.Port }
$ApiHostEff = $ApiHost
if ($ApiHostEff -eq 'localhost') { $ApiHostEff = '127.0.0.1' }   # без IPv6-резолва: демон слушает IPv4

if (-not $UvExe) {
    $cand = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $cand) { $UvExe = $cand }
    else {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { $UvExe = $cmd.Source } else { $UvExe = 'uv' }
    }
}
if (-not $OskardCommand) { $OskardCommand = '"{0}" run oskard' -f $UvExe }

if (-not $HermesHome) {
    if ($env:HERMES_HOME) { $HermesHome = $env:HERMES_HOME } else { $HermesHome = Join-Path $env:USERPROFILE 'hermes' }
}
$HermesExe = Join-Path $HermesHome 'bin\hermes.exe'
if (-not $HermesCommand) { $HermesCommand = '"{0}" gateway run' -f $HermesExe }
if (-not $HermesLog) { $HermesLog = Join-Path $DataDir 'hermes_gateway_sandbox.log' }
$HermesAvailable = $true
if (-not $NoHermes -and $HermesCommand -eq ('"{0}" gateway run' -f $HermesExe) -and -not (Test-Path -LiteralPath $HermesExe)) {
    $HermesAvailable = $false
}

# NO_PROXY: то, что уже задано у пользователя, + обязательный минимум запуска демона
# (loopback API и BingX — мимо прокси; Telegram — через прокси).
$requiredNoProxy = @('localhost', '127.0.0.1', '.bingx.com', 'bingx.com', 'github.com', 'api.github.com')
$noProxy = New-Object System.Collections.Generic.List[string]
foreach ($item in (([string]$env:NO_PROXY) -split ',') + $requiredNoProxy) {
    $t = $item.Trim()
    if ($t -and -not $noProxy.Contains($t)) { $noProxy.Add($t) }
}
$NoProxyValue = $noProxy -join ','

$uvDir = ''
if ($UvExe -match '[\\/]') { $uvDir = Split-Path -Parent $UvExe }
$childPath = $env:PATH
if ($uvDir -and -not ($childPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $uvDir.TrimEnd('\') })) {
    $childPath = "$uvDir;$childPath"
}

$OskardEnv = [ordered]@{
    PYTHONUTF8 = '1'
    PYTHONIOENCODING = 'utf-8'
    PYTHONUNBUFFERED = '1'
    NO_PROXY = $NoProxyValue
    PATH = $childPath
}
$HermesEnv = [ordered]@{
    PYTHONUTF8 = '1'
    PYTHONIOENCODING = 'utf-8'
    PYTHONUNBUFFERED = '1'
    NO_PROXY = $NoProxyValue
    PATH = $childPath
    HERMES_HOME = $HermesHome
}

# ======================================================================== процессы
function Update-ProcessSnapshot {
    try {
        $filter = "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe' OR Name='oskard.exe' OR Name='hermes.exe'"
        $script:ProcSnap = @(Get-CimInstance -ClassName Win32_Process -Filter $filter -ErrorAction Stop |
            Select-Object ProcessId, ParentProcessId, Name, CommandLine)
        $script:ProcSnapOk = $true
    } catch {
        $script:ProcSnap = @()
        $script:ProcSnapOk = $false
        Write-Log 'WARN' "снимок процессов (WMI) не получен: $($_.Exception.Message)"
    }
}

function Find-Procs([string]$Pattern) {
    return @($script:ProcSnap | Where-Object { $_.CommandLine -and $_.ProcessId -ne $PID -and $_.CommandLine -match $Pattern })
}

function Get-PortState {
    $listening = $false
    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        foreach ($ep in $listeners) { if ($ep.Port -eq $ApiPort) { $listening = $true; break } }
    } catch {
        Write-Log 'WARN' "таблица TCP не прочитана: $($_.Exception.Message)"
    }
    return @{ Listening = $listening }
}

function Get-PortOwner {
    try {
        $c = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($c) { return $c.OwningProcess }
    } catch { }
    return $null
}

function Format-Procs($Procs, $Port) {
    $parts = @()
    foreach ($p in @($Procs)) { $parts += ('{0}:{1}' -f $p.Name, $p.ProcessId) }
    if ($parts.Count -gt 0) { $s = 'процессы ' + ($parts -join ', ') } else { $s = 'процессов не видно' }
    if ($Port -and $Port.Listening) {
        $owner = Get-PortOwner
        if ($owner) { $s += "; порт $ApiPort слушает pid $owner" } else { $s += "; порт $ApiPort слушает" }
    }
    return $s
}

function Test-LockHeld([string]$Path) {
    # Тот же байт, что берет демон (msvcrt.locking, байт 0): занят = экземпляр жив/стартует.
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]'ReadWrite, Delete')
        try {
            $fs.Lock(0, 1)
            $fs.Unlock(0, 1)
            return $false
        } catch {
            return $true
        }
    } catch {
        Write-Log 'WARN' "lock-файл не открыт ($($_.Exception.Message)) — считаю занятым"
        return $true
    } finally {
        if ($fs) { $fs.Dispose() }
    }
}

function Start-Child([string]$CommandLine, [string]$WorkDir, [string]$LogPath, $EnvVars) {
    # cmd /c ">> log 2>&1": вывод пишет сам cmd, а не супервизор — его смерть не рвет
    # stdout демона. CreateNoWindow: своя консоль без окна (не всплывает Windows Terminal).
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $psi.Arguments = '/d /s /c "' + $CommandLine + ' >> "' + $LogPath + '" 2>&1"'
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    # Окружение — через свой процесс, а не psi.EnvironmentVariables: та строит словарь и
    # падает на дублях по регистру (Path/PATH — бывает при запуске из Git Bash).
    # Наследник получает блок окружения супервизора как есть.
    foreach ($k in $EnvVars.Keys) { [Environment]::SetEnvironmentVariable($k, [string]$EnvVars[$k], 'Process') }
    return [System.Diagnostics.Process]::Start($psi)
}

function Get-ExitMeaning($Code) {
    if ($null -eq $Code) { return 'код неизвестен' }
    switch ([int]$Code) {
        0 { return 'штатный выход' }
        2 { return 'отказ старта: caps.yaml не совпадает с пином CONSTITUTION.md (ст. 4)' }
        3 { return 'отказ старта: другой экземпляр держит lock/порт' }
        5 { return 'отказ старта: схема журнала не совпадает при живых сделках' }
        6 { return 'отказ старта: конфиг (режим/адрес API)' }
        -1073741510 { return 'прерван (Ctrl+C / закрытие консоли)' }
        default { return 'аварийный выход' }
    }
}

function Get-Backoff([int]$FailCount) {
    $n = [Math]::Max(0, $FailCount - 1)
    return [int][Math]::Min([double]$BackoffMaxSec, $BackoffBaseSec * [Math]::Pow(2, [Math]::Min($n, 20)))
}

function New-ProcState([string]$Name) {
    return @{
        Name = $Name; Proc = $null; WasAlive = $false; UpSince = $null; FailCount = 0
        NextStartAt = [datetime]::MinValue; DownAlerted = $false; Status = 'нет данных'
        LastHealthAt = [datetime]::MinValue; HealthFails = 0; HealthOk = $false; ApiDownAlerted = $false
        Unauthorized = $false; LastHbCheckAt = [datetime]::MinValue; HangAlerted = $false
        Ticks = $null; TicksAt = $null; LegacyWarned = $false
    }
}

function Reset-Health([hashtable]$S) {
    $S.HealthFails = 0; $S.HealthOk = $false; $S.ApiDownAlerted = $false; $S.Unauthorized = $false
    $S.HangAlerted = $false; $S.LastHealthAt = [datetime]::MinValue; $S.LastHbCheckAt = [datetime]::MinValue
    $S.Ticks = $null; $S.TicksAt = $null
}

function Receive-Exit([hashtable]$S) {
    if ($null -eq $S.Proc) { return $null }
    $exited = $true
    try { $exited = $S.Proc.HasExited } catch { $exited = $true }
    if (-not $exited) { return $null }
    $code = $null
    $procId = '?'
    try { $code = $S.Proc.ExitCode } catch { }
    try { $procId = $S.Proc.Id } catch { }
    try { $S.Proc.Dispose() } catch { }
    $S.Proc = $null
    Write-Log 'WARN' ("{0}: процесс (cmd pid {1}) завершился, код {2} — {3}" -f $S.Name, $procId, $code, (Get-ExitMeaning $code))
    return @{ Code = $code }
}

# ====================================================================== Telegram
function Get-TelegramConfig {
    $token = Get-Setting 'TELEGRAM_SIGNALS_BOT_TOKEN'
    $source = 'TELEGRAM_SIGNALS_BOT_TOKEN'
    if (-not $token) { $token = Get-Setting 'TELEGRAM_BOT_TOKEN'; $source = 'TELEGRAM_BOT_TOKEN' }
    $chat = Get-Setting 'TELEGRAM_CHAT_ID'
    if (-not $token -or -not $chat) { return $null }
    Add-Secret $token
    return @{ Token = $token; ChatId = $chat; Source = $source }
}

function Get-EnvProxyRaw {
    if ($TelegramProxy) { return $TelegramProxy }
    foreach ($n in @('HTTPS_PROXY', 'ALL_PROXY', 'HTTP_PROXY')) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ($v) { return $v.Trim() }
    }
    return ''
}

function New-EnvProxy([string]$Raw) {
    # .NET Framework умеет только http-прокси; логин:пароль из URL -> NetworkCredential
    if ($Raw -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $Raw = 'http://' + $Raw }
    $u = [Uri]$Raw
    if ($u.Scheme -ne 'http') { return $null }
    $wp = New-Object System.Net.WebProxy(('http://{0}:{1}' -f $u.Host, $u.Port))
    if ($u.UserInfo) {
        $parts = $u.UserInfo.Split(@(':'), 2)
        $user = [Uri]::UnescapeDataString($parts[0])
        $pass = ''
        if ($parts.Count -gt 1) { $pass = [Uri]::UnescapeDataString($parts[1]) }
        Add-Secret $pass
        $wp.Credentials = New-Object System.Net.NetworkCredential($user, $pass)
    }
    return $wp
}

function Get-TelegramRoutes {
    # Демон (httpx) ходит в Telegram через HTTPS_PROXY из окружения — сначала он же;
    # затем системный прокси Windows; в конце напрямую.
    $routes = @()
    $raw = Get-EnvProxyRaw
    if ($raw) {
        try {
            $wp = New-EnvProxy $raw
            if ($wp) { $routes += @{ Name = 'env-прокси'; Proxy = $wp } }
        } catch { Write-Log 'WARN' 'env-прокси не разобран (значение не печатаю)' }
    }
    try {
        $tg = [Uri]'https://api.telegram.org/'
        $sys = [System.Net.WebRequest]::GetSystemWebProxy()
        if (-not $sys.IsBypassed($tg) -and $sys.GetProxy($tg) -ne $tg) {
            $sys.Credentials = [System.Net.CredentialCache]::DefaultCredentials
            $routes += @{ Name = 'системный прокси'; Proxy = $sys }
        }
    } catch { }
    $routes += @{ Name = 'напрямую'; Proxy = $null }
    return $routes
}

function Send-Telegram([string]$Text) {
    if ($NoTelegram) { return $false }
    $cfg = Get-TelegramConfig
    if (-not $cfg) {
        if (-not $script:TelegramWarned) {
            Write-Log 'WARN' 'Telegram не настроен (TELEGRAM_SIGNALS_BOT_TOKEN/TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID): алерты только в лог'
            $script:TelegramWarned = $true
        }
        return $false
    }
    $body = @{ chat_id = $cfg.ChatId; text = $Text; disable_web_page_preview = $true } | ConvertTo-Json -Compress
    $bytes = $Utf8.GetBytes($body)
    $url = 'https://api.telegram.org/bot{0}/sendMessage' -f $cfg.Token
    foreach ($route in (Get-TelegramRoutes)) {
        $resp = $null
        try {
            $req = [System.Net.HttpWebRequest]::Create($url)
            $req.Method = 'POST'
            $req.ContentType = 'application/json; charset=utf-8'
            $req.Timeout = 10000
            $req.ReadWriteTimeout = 10000
            $req.KeepAlive = $false
            $req.Proxy = $route.Proxy
            $req.ContentLength = $bytes.Length
            $stream = $req.GetRequestStream()
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Close()
            $resp = $req.GetResponse()
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), $Utf8)
            $answer = $reader.ReadToEnd()
            $reader.Dispose()
            if ($answer -match '"ok"\s*:\s*true') {
                Write-Log 'INFO' "telegram: алерт отправлен ($($cfg.Source), $($route.Name))"
                return $true
            }
            Write-Log 'WARN' "telegram ($($route.Name)): ответ без ok: $(Protect-Text $answer)"
            return $false
        } catch {
            $ex = $_.Exception
            while ($ex -and -not ($ex -is [System.Net.WebException]) -and $ex.InnerException) { $ex = $ex.InnerException }
            if ($ex -is [System.Net.WebException] -and $ex.Response) {
                # ответил сам Telegram (400/401/403): другой маршрут не поможет
                $detail = ''
                try {
                    $r = New-Object System.IO.StreamReader($ex.Response.GetResponseStream(), $Utf8)
                    $detail = $r.ReadToEnd(); $r.Dispose()
                } catch { }
                $code = [int]$ex.Response.StatusCode
                $ex.Response.Close()
                if ($code -ne 407) {
                    Write-Log 'WARN' "telegram ($($route.Name)): HTTP $code $(Protect-Text $detail)"
                    return $false
                }
                Write-Log 'WARN' "telegram ($($route.Name)): прокси требует авторизацию (407)"
            } else {
                Write-Log 'WARN' "telegram ($($route.Name)): $(Protect-Text $ex.Message)"
            }
        } finally {
            if ($resp) { $resp.Close() }
        }
    }
    return $false
}

function Send-Alert([string]$Key, [string]$Text) {
    # Не чаще 1 сообщения в AlertCooldownMinutes на процесс; лишние события сворачиваются:
    # уйдет последнее состояние + счетчик пропущенных (Flush-Alerts).
    Write-Log 'ALERT' "[$Key] $Text"
    if ($DryRun) { return }
    $a = $script:Alerts[$Key]
    if ($null -eq $a) {
        $a = @{ LastSent = [datetime]::MinValue; Pending = $null; Folded = 0; RetryAt = [datetime]::MinValue }
        $script:Alerts[$Key] = $a
    }
    if ($null -ne $a.Pending) { $a.Folded++ }
    $a.Pending = $Text
    Invoke-AlertFlush $Key
}

function Invoke-AlertFlush([string]$Key) {
    $a = $script:Alerts[$Key]
    if ($null -eq $a -or $null -eq $a.Pending) { return }
    $now = Get-Date
    if (($now - $a.LastSent).TotalMinutes -lt $AlertCooldownMinutes -or $now -lt $a.RetryAt) { return }
    $msg = "OSKAR [$MachineName] $($a.Pending)"
    if ($a.Folded -gt 0) { $msg += "`n(и еще $($a.Folded) событ. до этого — подробности в data\supervisor.log)" }
    if (Send-Telegram $msg) {
        $a.LastSent = $now; $a.Pending = $null; $a.Folded = 0
    } else {
        $a.RetryAt = $now.AddSeconds(60)   # сеть/прокси: повтор через минуту, не каждую итерацию
    }
}

function Invoke-AllAlertFlush { foreach ($k in @($script:Alerts.Keys)) { Invoke-AlertFlush $k } }

# ==================================================================== keep-awake
function Set-KeepAwake([bool]$On) {
    # Запрос процесса (как у плеера), не настройка питания: снимается при выходе.
    try {
        if (-not ('OskarSupervisor.Power' -as [type])) {
            Add-Type -Namespace OskarSupervisor -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
        }
        $flags = [uint32]2147483648                       # ES_CONTINUOUS
        if ($On) { $flags = [uint32]2147483649 }           # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        $prev = [OskarSupervisor.Power]::SetThreadExecutionState($flags)
        if ($prev -eq 0) { Write-Log 'WARN' 'SetThreadExecutionState отказал' }
    } catch {
        Write-Log 'WARN' "keep-awake недоступен: $($_.Exception.Message)"
    }
}

# =================================================================== здоровье oskard
function Read-ApiToken {
    try {
        if (Test-Path -LiteralPath $TokenFile) {
            $t = [System.IO.File]::ReadAllText($TokenFile).Trim()
            Add-Secret $t
            return $t
        }
    } catch { }
    return ''
}

function Invoke-StatusOnce([string]$Token) {
    $resp = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create(('http://{0}:{1}/status' -f $ApiHostEff, $ApiPort))
        $req.Method = 'GET'
        $req.Timeout = $HealthTimeoutSec * 1000
        $req.ReadWriteTimeout = $HealthTimeoutSec * 1000
        $req.Proxy = $null            # loopback мимо любого прокси (VPN/системного)
        $req.KeepAlive = $false
        if ($Token) { $req.Headers.Add('X-Oskar-Token', $Token) }
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), $Utf8)
        $body = $reader.ReadToEnd()
        $reader.Dispose()
        $json = $null
        try { $json = $body | ConvertFrom-Json } catch { }
        return @{ Code = [int]$resp.StatusCode; Json = $json; Error = '' }
    } catch {
        $ex = $_.Exception
        while ($ex -and -not ($ex -is [System.Net.WebException]) -and $ex.InnerException) { $ex = $ex.InnerException }
        if ($ex -is [System.Net.WebException]) {
            if ($ex.Response) {
                $code = [int]$ex.Response.StatusCode
                $ex.Response.Close()
                return @{ Code = $code; Json = $null; Error = "HTTP $code" }
            }
            return @{ Code = 0; Json = $null; Error = [string]$ex.Status }
        }
        return @{ Code = 0; Json = $null; Error = (Protect-Text $_.Exception.Message) }
    } finally {
        if ($resp) { $resp.Close() }
    }
}

function Invoke-OskardStatus {
    # X-Oskar-Token: data\oskard.token (демон пишет при каждом старте), затем OSKAR_API_TOKEN —
    # тот же порядок, что у CLI `oskar status`. 401 -> пробуем следующий.
    $tokens = @()
    $ft = Read-ApiToken
    if ($ft) { $tokens += $ft }
    $et = Get-Setting 'OSKAR_API_TOKEN'
    if ($et) { Add-Secret $et; if ($tokens -notcontains $et) { $tokens += $et } }
    if ($tokens.Count -eq 0) { $tokens = @('') }
    $r = $null
    foreach ($t in $tokens) {
        $r = Invoke-StatusOnce $t
        if ($r.Code -ne 401) { break }
    }
    return $r
}

function Get-JsonValue($Obj, [string[]]$Path) {
    $cur = $Obj
    foreach ($p in $Path) {
        if ($null -eq $cur) { return $null }
        $prop = $cur.PSObject.Properties[$p]
        if ($null -eq $prop) { return $null }
        $cur = $prop.Value
    }
    return $cur
}

function Test-OskardHealth([datetime]$Now) {
    if (($Now - $O.LastHealthAt).TotalSeconds -lt $HealthEverySec) { return }
    $O.LastHealthAt = $Now
    $r = Invoke-OskardStatus
    if ($r.Code -eq 200 -or $r.Code -eq 401) {
        if ($r.Code -eq 401 -and -not $O.Unauthorized) {
            $O.Unauthorized = $true
            Write-Log 'WARN' 'API oskard отвечает 401: токен в data\oskard.token не подошел (демон жив, тики не видны)'
        }
        if (-not $O.HealthOk) {
            $runId = Get-JsonValue $r.Json @('run_id')
            Write-Log 'INFO' "oskard: API отвечает (HTTP $($r.Code), run_id=$runId)"
        }
        if ($O.ApiDownAlerted) {
            Send-Alert 'oskard' 'oskard: API снова отвечает.'
            $O.ApiDownAlerted = $false
        }
        $O.HealthOk = $true
        $O.HealthFails = 0
        $ticks = Get-JsonValue $r.Json @('shadow', 'ticks')
        if ($null -ne $ticks -and $ticks -ne $O.Ticks) { $O.Ticks = $ticks; $O.TicksAt = $Now }
        return
    }
    if ($null -ne $O.UpSince -and ($Now - $O.UpSince).TotalSeconds -lt $StartupGraceSec) {
        Write-Log 'INFO' ("oskard: API пока не отвечает ({0}), старт {1:N0} с назад" -f $r.Error, ($Now - $O.UpSince).TotalSeconds)
        return
    }
    $O.HealthOk = $false
    $O.HealthFails++
    Write-Log 'WARN' "oskard: API не ответил ($($O.HealthFails)/$HealthFailThreshold): $($r.Error)"
    if ($O.HealthFails -ge $HealthFailThreshold -and -not $O.ApiDownAlerted) {
        $O.ApiDownAlerted = $true
        Send-Alert 'oskard' ("oskard: процесс жив, но API {0}:{1} не отвечает {2} раз подряд ({3}). Авто-kill не делаю — проверь data\oskard.log." -f $ApiHostEff, $ApiPort, $O.HealthFails, $r.Error)
    }
}

function Find-LastHeartbeat([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $text = ''
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            [System.IO.FileShare]'ReadWrite, Delete')
        $take = [int][Math]::Min($fs.Length, [long]$HeartbeatTailBytes)
        if ($take -le 0) { return $null }
        [void]$fs.Seek(-$take, [System.IO.SeekOrigin]::End)
        $buf = New-Object byte[] $take
        $read = 0
        while ($read -lt $take) {
            $n = $fs.Read($buf, $read, $take - $read)
            if ($n -le 0) { break }
            $read += $n
        }
        $text = $Utf8.GetString($buf, 0, $read)
    } catch {
        return $null
    } finally {
        if ($fs) { $fs.Dispose() }
    }
    $ms = [regex]::Matches($text, $HeartbeatPattern)
    if ($ms.Count -eq 0) { return $null }
    $stamp = $ms[$ms.Count - 1].Groups[1].Value
    return [datetime]::ParseExact($stamp, 'yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
}

function Get-LastHeartbeat {
    foreach ($p in @($OskardLog, "$OskardLog.1")) {   # .1 — лог только что ротирован
        $ts = Find-LastHeartbeat $p
        if ($null -ne $ts) { return $ts }
    }
    return $null
}

function Test-OskardHeartbeat([datetime]$Now) {
    if (($Now - $O.LastHbCheckAt).TotalSeconds -lt 60) { return }
    $O.LastHbCheckAt = $Now
    $hb = Get-LastHeartbeat
    if ($null -ne $hb) {
        $last = $hb; $src = 'heartbeat в data\oskard.log'
    } elseif ($null -ne $O.TicksAt) {
        # демон на коде до heartbeat (старый экземпляр): временно смотрим на рост тиков /status
        if (-not $O.LegacyWarned) {
            $O.LegacyWarned = $true
            Write-Log 'WARN' 'в data\oskard.log нет строк heartbeat (демон на старом коде?) — до рестарта сторож смотрит на тики /status'
        }
        $last = $O.TicksAt; $src = 'тики /status'
    } else {
        $last = $O.UpSince; $src = 'начало наблюдения'
    }
    if ($null -eq $last) { return }
    # старый heartbeat прошлого запуска не должен поднимать тревогу сразу после старта
    if ($null -ne $O.UpSince -and $last -lt $O.UpSince) { $last = $O.UpSince }
    $ageMin = ($Now - $last).TotalMinutes
    if ($ageMin -gt $HangMinutes) {
        if (-not $O.HangAlerted) {
            $O.HangAlerted = $true
            Send-Alert 'oskard' ("oskard завис? Нет признаков жизни {0:N0} мин (порог {1} мин; источник: {2}). Процесс жив, авто-kill не делаю — проверь data\oskard.log и /status." -f $ageMin, $HangMinutes, $src)
        }
    } elseif ($O.HangAlerted) {
        $O.HangAlerted = $false
        Send-Alert 'oskard' 'oskard ожил: heartbeat снова идет.'
    }
}

# ==================================================================== шаги надзора
function Start-Oskard([datetime]$Now) {
    Invoke-LogRotation $OskardStdout $ChildLogMaxBytes 3
    Add-Marker $OskardStdout ('=== oskard start by supervisor {0} (attempt {1}) ===' -f (Get-UtcStamp), ($O.FailCount + 1))
    try {
        $p = Start-Child $OskardCommand $RepoRoot $OskardStdout $OskardEnv
        $O.Proc = $p
        $O.WasAlive = $true
        $O.UpSince = $Now
        Reset-Health $O
        $O.Status = "запущен супервизором (cmd pid $($p.Id))"
        Write-Log 'INFO' "oskard запущен: cmd pid $($p.Id), вывод -> $OskardStdout"
    } catch {
        $O.FailCount++
        $delay = Get-Backoff $O.FailCount
        $O.NextStartAt = $Now.AddSeconds($delay)
        $O.Status = 'не запустился'
        $O.DownAlerted = $true
        Send-Alert 'oskard' "не смог запустить oskard: $($_.Exception.Message). Повтор через $delay с."
    }
}

function Step-Oskard {
    $now = Get-Date
    $exit = Receive-Exit $O
    $own = $null -ne $O.Proc
    $procs = @(Find-Procs $OskardProcessPattern)
    $port = Get-PortState
    $alive = $own -or ($procs.Count -gt 0) -or $port.Listening

    if ($alive) {
        if (-not $O.WasAlive) {
            $O.WasAlive = $true
            $O.UpSince = $now
            Reset-Health $O
            $desc = Format-Procs $procs $port
            $O.Status = "уже работал ($desc)"
            Write-Log 'INFO' "oskard уже работает ($desc) — второй не запускаю, наблюдаю"
        }
        if ($O.FailCount -gt 0 -and ($now - $O.UpSince).TotalMinutes -ge $StableResetMinutes) {
            $O.FailCount = 0
            Write-Log 'INFO' "oskard стабилен $StableResetMinutes мин — задержка рестарта сброшена"
        }
        Test-OskardHealth $now
        Test-OskardHeartbeat $now
        if ($O.DownAlerted -and $O.HealthOk) {
            $O.DownAlerted = $false
            Send-Alert 'oskard' 'oskard снова работает: API отвечает.'
        }
        return
    }

    if ($O.WasAlive) {
        $O.WasAlive = $false
        $O.UpSince = $null
        $O.FailCount++
        $delay = Get-Backoff $O.FailCount
        $O.NextStartAt = $now.AddSeconds($delay)
        if ($exit -and $exit.Code -eq 3) {
            $O.Status = 'отказ старта: lock/порт заняты'
            Write-Log 'WARN' "oskard: другой экземпляр держит lock/порт — повтор через $delay с"
        } else {
            if ($exit) { $why = "код выхода $($exit.Code) — $(Get-ExitMeaning $exit.Code)" }
            else { $why = 'процесс исчез (внешний экземпляр завершился или убит)' }
            $O.Status = "упал: $why"
            $O.DownAlerted = $true
            Send-Alert 'oskard' "oskard упал: $why. Перезапуск через $delay с (попытка $($O.FailCount))."
        }
    }
    if ($now -lt $O.NextStartAt) { return }
    if (-not (Test-TradingHost)) { $O.NextStartAt = $now.AddSeconds($BackoffMaxSec); return }
    if (Test-LockHeld $LockFile) {
        $O.Status = 'lock занят, процесса не видно'
        Write-Log 'WARN' 'oskard: data\oskard.lock занят, а процесса и порта не видно — не запускаю (instance lock), проверю через 30 с'
        $O.NextStartAt = $now.AddSeconds(30)
        return
    }
    Start-Oskard $now
}

function Start-Hermes([datetime]$Now) {
    Invoke-LogRotation $HermesLog $ChildLogMaxBytes 3
    Add-Marker $HermesLog ('=== gateway start by supervisor {0} (attempt {1}) ===' -f (Get-UtcStamp), ($H.FailCount + 1))
    try {
        $p = Start-Child $HermesCommand $HermesHome $HermesLog $HermesEnv
        $H.Proc = $p
        $H.WasAlive = $true
        $H.UpSince = $Now
        $H.Status = "запущен супервизором (cmd pid $($p.Id))"
        Write-Log 'INFO' "hermes gateway запущен: cmd pid $($p.Id), вывод -> $HermesLog"
    } catch {
        $H.FailCount++
        $delay = Get-Backoff $H.FailCount
        $H.NextStartAt = $Now.AddSeconds($delay)
        $H.Status = 'не запустился'
        $H.DownAlerted = $true
        Send-Alert 'hermes' "не смог запустить hermes gateway: $($_.Exception.Message). Повтор через $delay с."
    }
}

function Step-Hermes {
    if ($NoHermes) { $H.Status = 'выключен (-NoHermes)'; return }
    if (-not $HermesAvailable) {
        if ($H.Status -ne 'нет hermes.exe') { Write-Log 'WARN' "hermes.exe не найден ($HermesExe) — шлюз не надзираю" }
        $H.Status = 'нет hermes.exe'
        return
    }
    $now = Get-Date
    $exit = Receive-Exit $H
    $own = $null -ne $H.Proc
    if (-not $own -and -not $script:ProcSnapOk) {
        # без снимка процессов не видно чужой шлюз: второй getUpdates = 409 Conflict у бота
        $H.Status = 'неизвестно (нет снимка процессов)'
        return
    }
    $procs = @(Find-Procs $HermesProcessPattern)
    $alive = $own -or ($procs.Count -gt 0)

    if ($alive) {
        if (-not $H.WasAlive) {
            $H.WasAlive = $true
            $H.UpSince = $now
            $desc = Format-Procs $procs $null
            $H.Status = "уже работал ($desc)"
            Write-Log 'INFO' "hermes gateway уже работает ($desc) — второй не запускаю, наблюдаю"
        }
        $upSec = ($now - $H.UpSince).TotalSeconds
        if ($H.FailCount -gt 0 -and $upSec -ge $StableResetMinutes * 60) {
            $H.FailCount = 0
            Write-Log 'INFO' "hermes стабилен $StableResetMinutes мин — задержка рестарта сброшена"
        }
        if ($H.DownAlerted -and $upSec -ge $HermesStableSec) {
            $H.DownAlerted = $false
            Send-Alert 'hermes' 'hermes gateway снова работает.'
        }
        return
    }

    if ($H.WasAlive) {
        $H.WasAlive = $false
        $H.UpSince = $null
        $H.FailCount++
        $delay = Get-Backoff $H.FailCount
        $H.NextStartAt = $now.AddSeconds($delay)
        if ($exit) { $why = "код выхода $($exit.Code)" } else { $why = 'процесс исчез' }
        $H.Status = "упал: $why"
        $H.DownAlerted = $true
        Send-Alert 'hermes' "hermes gateway упал ($why). Перезапуск через $delay с (попытка $($H.FailCount))."
    }
    if ($now -lt $H.NextStartAt) { return }
    if (-not (Test-TradingHost)) { $H.NextStartAt = $now.AddSeconds($BackoffMaxSec); return }
    Start-Hermes $now
}

function Get-NextWakeSeconds {
    $sleep = [double]$IntervalSec
    $now = Get-Date
    foreach ($s in @($O, $H)) {
        if (-not $s.WasAlive -and $s.NextStartAt -gt $now) {
            $sleep = [Math]::Min($sleep, ($s.NextStartAt - $now).TotalSeconds)
        }
    }
    return [int][Math]::Max(1, [Math]::Ceiling($sleep))
}

# ========================================================================= DRY RUN
function Show-DryRun {
    Write-Host '=== OSKAR supervisor: DRY RUN (ничего не запускаю, в сеть и к API не хожу) ==='
    $th = Get-TradingHost
    if (-not $th) { $guard = 'файла нет — проверка выключена' }
    elseif ($th -eq $MachineName) { $guard = "$th -> этот ПК, OK" }
    else { $guard = "$th -> ЭТОТ ПК НЕ ТОРГОВЫЙ: супервизор не запустит ничего и выйдет" }
    Write-Host "ПК: $MachineName; торговый ПК (scripts\trading_host.txt): $guard"
    Write-Host "Репозиторий: $RepoRoot"
    $other = $null
    $busy = $false
    try { $busy = [System.Threading.Mutex]::TryOpenExisting($MutexName, [ref]$other) } catch { $busy = $true }
    if ($other) { $other.Dispose() }
    if ($busy) { $sv = 'ДА — второй экземпляр сразу выйдет' } else { $sv = 'нет' }
    Write-Host "Другой супервизор уже работает: $sv"
    Write-Host "Лог супервизора: $SupLog (ротация $MaxLogMB МБ x 3)"
    Write-Host "Цикл: каждые $IntervalSec с; рестарт: $BackoffBaseSec с -> $BackoffMaxSec с, сброс после $StableResetMinutes мин; алерты не чаще 1 в $AlertCooldownMinutes мин на процесс"
    Write-Host "Keep-awake: SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED) каждые $KeepAwakeMinutes мин (экран может гаснуть)"

    $cfg = Get-TelegramConfig
    if ($NoTelegram) { $tgs = 'выключен (-NoTelegram)' }
    elseif ($cfg) { $tgs = "$($cfg.Source) + TELEGRAM_CHAT_ID заданы" }
    else { $tgs = 'НЕ настроен (нет токена/chat id в окружении и .env) — алерты только в лог' }
    Write-Host "Telegram: $tgs"
    $routes = @()
    $raw = Get-EnvProxyRaw
    if ($raw) {
        $kind = 'http'
        if ($raw -match '^([A-Za-z][A-Za-z0-9+.-]*)://') { $kind = $Matches[1] }
        $auth = ''
        if ($raw -match '://[^/@]+@') { $auth = ', с авторизацией' }
        $routes += "env-прокси ($kind$auth)"
    }
    $routes += 'системный прокси (если включен)', 'напрямую'
    Write-Host "  маршрут Telegram: $($routes -join ' -> ')"

    Update-ProcessSnapshot
    Write-Host ''
    Write-Host '[oskard]'
    Write-Host ("  команда: cmd.exe /d /s /c `"{0} >> `"{1}`" 2>&1`"" -f $OskardCommand, $OskardStdout)
    Write-Host "  каталог: $RepoRoot"
    Write-Host ("  env: PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 PATH=+{0}" -f $uvDir)
    Write-Host "  env: NO_PROXY=$NoProxyValue"
    if (Test-Path -LiteralPath $TokenFile) { $tok = 'файл есть' } else { $tok = 'файла пока нет' }
    Write-Host "  health: GET http://${ApiHostEff}:$ApiPort/status, X-Oskar-Token из data\oskard.token ($tok), таймаут $HealthTimeoutSec с, без прокси; льгота на старт $StartupGraceSec с"
    $hb = Get-LastHeartbeat
    if ($null -ne $hb) { $hbs = "последний {0:yyyy-MM-dd HH:mm:ss} ({1:N0} мин назад)" -f $hb, ((Get-Date) - $hb).TotalMinutes }
    else { $hbs = 'строк heartbeat нет' }
    Write-Host "  сторож: heartbeat в $OskardLog, порог $HangMinutes мин; $hbs"
    $procs = @(Find-Procs $OskardProcessPattern)
    $port = Get-PortState
    Write-Host "  сейчас: $(Format-Procs $procs $port)"
    if ($procs.Count -gt 0 -or $port.Listening) {
        Write-Host '  план: запуск ПРОПУЩЕН — экземпляр уже есть; наблюдать (health + heartbeat), рестарт только после его смерти'
    } else {
        Write-Host '  план: проверить data\oskard.lock и запустить (в DRY RUN lock не трогаю)'
    }

    Write-Host ''
    Write-Host '[hermes gateway]'
    if ($NoHermes) { Write-Host '  выключен (-NoHermes)' }
    elseif (-not $HermesAvailable) { Write-Host "  hermes.exe не найден: $HermesExe — надзора не будет" }
    else {
        Write-Host ("  команда: cmd.exe /d /s /c `"{0} >> `"{1}`" 2>&1`"" -f $HermesCommand, $HermesLog)
        Write-Host "  каталог: $HermesHome"
        Write-Host "  env: HERMES_HOME=$HermesHome PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 NO_PROXY=(как у oskard)"
        $hp = @(Find-Procs $HermesProcessPattern)
        Write-Host "  сейчас: $(Format-Procs $hp $null)"
        if ($hp.Count -gt 0) { Write-Host '  план: запуск ПРОПУЩЕН — шлюз уже работает; наблюдать' }
        else { Write-Host '  план: запустить' }
    }
    Write-Host '=== конец DRY RUN ==='
}

# ======================================================================= main
$O = New-ProcState 'oskard'
$H = New-ProcState 'hermes'

if ($DryRun) {
    Show-DryRun
    exit 0
}

if (-not (Test-TradingHost)) {
    Write-Log 'ERROR' ("этот ПК ({0}) не торговый (scripts\trading_host.txt: {1}) — супервизор выходит" -f $MachineName, (Get-TradingHost))
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch {
    $ex = $_.Exception
    if ($ex -is [System.Threading.AbandonedMutexException] -or $ex.InnerException -is [System.Threading.AbandonedMutexException]) {
        $owned = $true   # прошлый супервизор умер, не отпустив mutex
    } else { throw }
}
if (-not $owned) {
    Write-Log 'WARN' "уже работает другой супервизор ($MutexName) — выхожу"
    $mutex.Dispose()
    exit 0
}

try {
    Write-Log 'INFO' ("супервизор запущен: pid {0}, ПК {1}, repo {2}, API {3}:{4}, режим {5}" -f $PID, $MachineName, $RepoRoot, $ApiHostEff, $ApiPort, $(if ($Once) { 'Once' } else { 'loop' }))
    Set-KeepAwake $true
    $awakeAt = Get-Date
    $iteration = 0
    while ($true) {
        $iteration++
        try {
            Update-ProcessSnapshot
            Step-Oskard
            Step-Hermes
            Invoke-AllAlertFlush
            if ($iteration -eq 1) {
                $summary = "oskard: $($O.Status); hermes: $($H.Status)"
                Write-Log 'INFO' "итерация 1: $summary"
                if (-not $Once) { Send-Alert 'supervisor' "Супервизор запущен. $summary" }
            }
            if (((Get-Date) - $awakeAt).TotalMinutes -ge $KeepAwakeMinutes) {
                Set-KeepAwake $true
                $awakeAt = Get-Date
            }
        } catch {
            Write-Log 'ERROR' ("итерация {0} упала: {1} (строка {2})" -f $iteration, $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber)
        }
        if ($Once -or ($MaxIterations -gt 0 -and $iteration -ge $MaxIterations)) { break }
        Start-Sleep -Seconds (Get-NextWakeSeconds)
    }
} finally {
    Set-KeepAwake $false
    Write-Log 'INFO' "супервизор остановлен (oskard: $($O.Status); hermes: $($H.Status)) — дочерние процессы продолжают работать"
    try { $mutex.ReleaseMutex() } catch { }
    $mutex.Dispose()
}
exit 0
