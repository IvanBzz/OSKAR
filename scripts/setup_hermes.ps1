# ============================================================================
# Установка и настройка Hermes для OSKAR — запускать из СВОЕГО терминала
# (не из агентской сессии: песочница Claude Desktop виртуализирует установки).
#
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Ivan\Desktop\Projects\Trading\OSKAR\scripts\setup_hermes.ps1"
#
# Делает: официальный установщик Hermes -> телеграм-конфиг из OSKAR\.env ->
# скиллы oskar-* -> печатает следующие шаги (auth + gateway).
# ============================================================================

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$OskarEnv = Join-Path $RepoRoot ".env"

Write-Host "=== OSKAR: установка Hermes ===" -ForegroundColor Cyan

# --- 0. Секреты из OSKAR\.env ------------------------------------------------
if (-not (Test-Path $OskarEnv)) {
    Write-Host "ОШИБКА: нет $OskarEnv (токен бота обязан быть там)" -ForegroundColor Red
    exit 1
}
$envText = Get-Content $OskarEnv -Raw
$botToken = if ($envText -match 'TELEGRAM_BOT_TOKEN="?([^"\r\n]+)"?') { $Matches[1] } else { "" }
$chatId   = if ($envText -match 'TELEGRAM_CHAT_ID="?([^"\r\n]+)"?') { $Matches[1] } else { "" }
if (-not $botToken -or -not $chatId) {
    Write-Host "ОШИБКА: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заполнены в OSKAR\.env" -ForegroundColor Red
    exit 1
}
Write-Host "[ok] токен бота и chat_id прочитаны из OSKAR\.env"

# --- 1. Официальный установщик (в стандартный %LOCALAPPDATA% — вне песочницы
#        это РЕАЛЬНЫЙ путь) ---------------------------------------------------
$installer = Join-Path $env:TEMP "hermes_install.ps1"
Invoke-WebRequest -Uri "https://hermes-agent.nousresearch.com/install.ps1" -OutFile $installer
& powershell -ExecutionPolicy Bypass -File $installer -NonInteractive -SkipSetup -SkipComputerUse
if ($LASTEXITCODE -ne 0) {
    Write-Host "ОШИБКА: установщик Hermes завершился с кодом $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

# Установщик уважает HERMES_HOME (user env) — определяем фактический дом
$HermesHome = [System.Environment]::GetEnvironmentVariable("HERMES_HOME", "User")
if (-not $HermesHome) { $HermesHome = "$env:LOCALAPPDATA\hermes" }
Write-Host "[ok] HERMES_HOME = $HermesHome"

# --- 2. Telegram-конфиг ------------------------------------------------------
$hermesEnv = Join-Path $HermesHome ".env"
$c = Get-Content $hermesEnv -Raw
$c = $c -replace '# TELEGRAM_BOT_TOKEN=.*', "TELEGRAM_BOT_TOKEN=$botToken"
$c = $c -replace '# TELEGRAM_ALLOWED_USERS=(\s+)#.*', "TELEGRAM_ALLOWED_USERS=$chatId"
$c = $c -replace '# TELEGRAM_HOME_CHANNEL=(\s+)#.*', "TELEGRAM_HOME_CHANNEL=$chatId"
[System.IO.File]::WriteAllText($hermesEnv, $c, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[ok] Telegram: токен вписан, allowlist ограничен chat_id=$chatId"

# --- 3. Скиллы OSKAR ---------------------------------------------------------
foreach ($skill in @("oskar-control", "oskar-approvals")) {
    Copy-Item (Join-Path $RepoRoot "skills\$skill") (Join-Path $HermesHome "skills\") -Recurse -Force
    # скрипт скилла ищет токен API в <OSKAR>/data/oskard.token — из копии путь не вычислить
    $marker = Join-Path $HermesHome "skills\$skill\scripts\oskar_home.txt"
    [System.IO.File]::WriteAllText($marker, $RepoRoot, (New-Object System.Text.UTF8Encoding($false)))
}
Write-Host "[ok] скиллы oskar-control и oskar-approvals установлены"

# --- 4. Следующие шаги -------------------------------------------------------
Write-Host ""
Write-Host "=== ГОТОВО. Дальше (в НОВОМ терминале, чтобы подхватился PATH): ===" -ForegroundColor Green
Write-Host "  1) hermes auth add nous      # вход через Nous Portal (браузер, один клик)"
Write-Host "  2) hermes gateway            # запуск telegram-gateway"
Write-Host "  Проверка: напиши боту в телеге 'статус' — он должен ответить."
