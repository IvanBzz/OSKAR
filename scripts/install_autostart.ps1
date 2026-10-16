<#
.SYNOPSIS
  Регистрирует (или снимает) задачу Task Scheduler «OSKAR Supervisor» — автозапуск
  scripts\supervise.ps1 при входе пользователя. Запускать из СВОЕГО терминала (не из
  сессии Claude), права администратора не нужны:

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall

.DESCRIPTION
  Задача: триггер «при входе <текущий пользователь>» с задержкой 1 мин; без ограничения
  времени выполнения; при сбое — перезапуск каждую минуту (до 999 раз); второй экземпляр
  не запускается; обычный приоритет (по умолчанию Task Scheduler дает «ниже обычного»,
  и его наследовали бы демон и шлюз); работает и от батареи.

  Действие: powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File supervise.ps1,
  обернутое в «conhost.exe --headless»: в Windows 11 терминал по умолчанию — Windows
  Terminal, и -WindowStyle Hidden его окно не прячет (остается пустое окно, закрытие
  которого убивает супервизор). -NoConhost — без обертки, ровно команда powershell.

  Только на торговом ПК (scripts\trading_host.txt): на второй машине установка отказывает.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$TaskName = 'OSKAR Supervisor',
    [int]$DelayMinutes = 1,
    [switch]$NoConhost
)

$ErrorActionPreference = 'Stop'
$Supervisor = Join-Path $PSScriptRoot 'supervise.ps1'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TradingHostFile = Join-Path $PSScriptRoot 'trading_host.txt'
$UserId = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "=== OSKAR: автозапуск супервизора ($TaskName) ===" -ForegroundColor Cyan

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "[ok] задачи «$TaskName» нет — снимать нечего"
        exit 0
    }
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
        Write-Host '[ok] супервизор остановлен (Stop-ScheduledTask)'
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[ok] задача «$TaskName» удалена"
    Write-Host ''
    Write-Host 'oskard и hermes gateway, запущенные супервизором, ПРОДОЛЖАЮТ работать.' -ForegroundColor Yellow
    Write-Host 'Остановить их — см. scripts\README_supervisor.md, раздел «Остановить».'
    exit 0
}

# --- торговый ПК: демон только на одной машине (урок v1: два экземпляра = двойная позиция)
$tradingHost = ''
if (Test-Path -LiteralPath $TradingHostFile) {
    $tradingHost = Get-Content -LiteralPath $TradingHostFile -Encoding UTF8 |
        ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -First 1
}
if ($tradingHost -and $tradingHost -ne $env:COMPUTERNAME) {
    Write-Host "ОТКАЗ: этот ПК — $env:COMPUTERNAME, а торговый по scripts\trading_host.txt — $tradingHost." -ForegroundColor Red
    Write-Host 'Демон работает только на ОДНОЙ машине. Если переносишь торговлю сюда: останови демон и'
    Write-Host 'супервизор на старом ПК (install_autostart.ps1 -Uninstall), впиши сюда имя этого ПК, закоммить.'
    exit 1
}
if (-not (Test-Path -LiteralPath $Supervisor)) {
    Write-Host "ОШИБКА: нет $Supervisor" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.env'))) {
    Write-Host 'ВНИМАНИЕ: нет .env — демон без ключей, алерты супервизора не уйдут в Telegram' -ForegroundColor Yellow
}

# --- действие
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$psArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $Supervisor
$conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
if (-not $NoConhost -and (Test-Path -LiteralPath $conhost)) {
    $execute = $conhost
    $arguments = '--headless "{0}" {1}' -f $psExe, $psArgs
} else {
    $execute = $psExe
    $arguments = $psArgs
}
$action = New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $RepoRoot

# --- триггер: вход этого пользователя + задержка
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$trigger.Delay = 'PT{0}M' -f $DelayMinutes

# --- настройки
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Priority 4

$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

$existed = [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description 'OSKAR 24/7: supervise.ps1 держит живыми oskard и hermes gateway, алерты в Telegram. Лог: data\supervisor.log' |
    Out-Null

$verb = 'создана'
if ($existed) { $verb = 'обновлена' }
Write-Host "[ok] задача «$TaskName» $verb"
Write-Host "     пользователь:  $UserId (только пока он вошел в систему, без пароля, без админ-прав)"
Write-Host "     триггер:       при входе, задержка $DelayMinutes мин"
Write-Host "     действие:      $execute $arguments"
Write-Host "     рабочий каталог: $RepoRoot"
Write-Host '     при сбое:      перезапуск каждую 1 мин, до 999 раз'
Write-Host '     лимит времени: нет; второй экземпляр: не запускать; приоритет: обычный; от батареи: да'
Write-Host ''
Write-Host '=== Дальше ===' -ForegroundColor Green
Write-Host "Запустить сейчас, не перезаходя в систему (подхватит уже работающие oskard/hermes):"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host 'Проверить:'
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "  Get-Content '$RepoRoot\data\supervisor.log' -Tail 20"
Write-Host 'Снять автозапуск:'
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File '$PSCommandPath' -Uninstall"
