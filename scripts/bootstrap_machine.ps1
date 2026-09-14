# ============================================================================
# Развертывание OSKAR на НОВОЙ машине (ноутбук). Запускать в своем терминале:
#
#   powershell -ExecutionPolicy Bypass -File bootstrap_machine.ps1 -Target "C:\Users\<user>\Desktop\Projects\Trading"
#
# Что делает: клонирует репо (КОД + VAULT в одном месте), ставит uv-окружение,
# прогоняет тесты, печатает что осталось перенести руками (секреты).
#
# ВАЖНО (Конституция, ст. про single instance): демон oskard должен работать
# ТОЛЬКО НА ОДНОЙ машине одновременно. Урок v1: два экземпляра на одном
# аккаунте слили ADA в двойную позицию. Ноутбук — для разработки и чтения
# vault; торгует та машина, где запущен демон.
# ============================================================================

param(
    [string]$Target = "$env:USERPROFILE\Desktop\Projects\Trading",
    [string]$Repo = "https://github.com/IvanBzz/OSKAR.git"
)

$ErrorActionPreference = "Stop"
Write-Host "=== OSKAR: развертывание на этой машине ===" -ForegroundColor Cyan

# --- 1. git ------------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "ОШИБКА: нет git. Поставь: winget install Git.Git" -ForegroundColor Red
    exit 1
}

$dest = Join-Path $Target "OSKAR"
if (Test-Path (Join-Path $dest ".git")) {
    Write-Host "[ok] репозиторий уже есть, обновляю"
    Push-Location $dest
    git pull --ff-only
    Pop-Location
} else {
    New-Item -ItemType Directory -Force $Target | Out-Null
    git clone $Repo $dest
    Write-Host "[ok] репозиторий склонирован: $dest"
}

# --- 2. uv + окружение -------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "-> ставлю uv"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
}
Push-Location $dest
uv sync --all-groups
Write-Host "[ok] зависимости установлены"

# --- 3. проверка -------------------------------------------------------------
uv run pytest -m "not vst" -q
$testsOk = $LASTEXITCODE -eq 0
uv run oskar caps-verify
Pop-Location

# --- 4. что осталось ---------------------------------------------------------
Write-Host ""
Write-Host "=== ГОТОВО ===" -ForegroundColor Green
Write-Host "Тесты: $(if ($testsOk) { 'зеленые' } else { 'ПАДАЮТ - разобраться' })"
Write-Host ""
Write-Host "Obsidian: открой как vault папку  $dest\vault"
Write-Host "  (vault лежит В РЕПОЗИТОРИИ - синхронизируется обычным git pull/push)"
Write-Host ""
Write-Host "Перенести РУКАМИ со старой машины (в git их нет и не будет):" -ForegroundColor Yellow
Write-Host "  .env        - ключи BingX, токены ботов, CLAUDE_CODE_OAUTH_TOKEN"
Write-Host "  data\*.db   - журнал сделок (только если переносишь и торговлю)"
Write-Host ""
Write-Host "Демон запускать ТОЛЬКО на одной машине одновременно (ст. single instance)." -ForegroundColor Yellow
Write-Host "Ежедневная синхронизация: git pull (утром) / git push (вечером)."
