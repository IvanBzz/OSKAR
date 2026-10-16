"""Супервизор 24/7 (блок G): scripts/supervise.ps1 и scripts/install_autostart.ps1.

- оба скрипта разбираются парсером Windows PowerShell 5.1 без ошибок;
- оба в UTF-8 с BOM: без BOM PS 5.1 читает файл как ANSI (cp1251), и кириллица в
  строках ломается (алерты в Telegram превращаются в кракозябры);
- DRY RUN ничего не пишет и не запускает, токен бота и пароль прокси не печатает;
- строка heartbeat shadow-потока совпадает с шаблоном сторожа ($HeartbeatPattern).

PowerShell-проверки — только на Windows (skip в остальных местах).
"""

import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from oskar.logging_setup import LOG_FORMAT
from tests.unit.test_runner_heartbeat import make

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SUPERVISOR = SCRIPTS / "supervise.ps1"
INSTALLER = SCRIPTS / "install_autostart.ps1"
POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None
windows_only = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell 5.1")

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
PROXY_PASSWORD = "s3cretProxyPass"


def _text(raw: bytes) -> str:
    # вывод PS в пайп — в кодовой странице консоли; проверяем только ASCII-маркеры
    return raw.decode("utf-8", errors="replace")


@windows_only
@pytest.mark.parametrize("script", [SUPERVISOR, INSTALLER], ids=lambda p: p.name)
def test_powershell_parser_finds_no_errors(script):
    command = (
        "$t = $null; $e = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$t, [ref]$e); "
        "$e | ForEach-Object { 'line ' + $_.Extent.StartLineNumber + ': ' + $_.Message }; "
        "exit @($e).Count"
    )
    r = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, timeout=120,
    )
    assert r.returncode == 0, _text(r.stdout) + _text(r.stderr)


@pytest.mark.parametrize("script", [SUPERVISOR, INSTALLER], ids=lambda p: p.name)
def test_scripts_are_utf8_with_bom(script):
    raw = script.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "PS 5.1 без BOM читает кириллицу как cp1251"
    raw.decode("utf-8")


@windows_only
def test_dry_run_writes_nothing_and_hides_secrets(tmp_path):
    (tmp_path / ".env").write_text(
        f"TELEGRAM_SIGNALS_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_CHAT_ID=4242\n", encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("TELEGRAM_")}
    env["HTTPS_PROXY"] = f"http://user:{PROXY_PASSWORD}@10.255.0.1:3128"
    r = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SUPERVISOR), "-DryRun", "-Once", "-RepoRoot", str(tmp_path), "-NoHermes"],
        capture_output=True, timeout=180, env=env,
    )
    out = _text(r.stdout) + _text(r.stderr)
    assert r.returncode == 0, out
    assert "DRY RUN" in out
    assert "TELEGRAM_SIGNALS_BOT_TOKEN" in out  # что настроено — по имени переменной
    assert BOT_TOKEN.split(":")[1] not in out
    assert PROXY_PASSWORD not in out
    assert "/status" in out and "run oskard" in out
    # ни supervisor.log, ни маркеров в логах, ни lock: dry run не создает даже data/
    assert not (tmp_path / "data").exists()


def test_heartbeat_line_matches_supervisor_watchdog(tmp_path, caplog):
    """Контракт: shadow_runner.heartbeat() + logging_setup.LOG_FORMAT -> строка, которую
    находит $HeartbeatPattern сторожа, а время из нее разбирается как в supervise.ps1."""
    source = SUPERVISOR.read_text(encoding="utf-8-sig")
    m = re.search(r"^\$HeartbeatPattern = '([^']+)'", source, re.MULTILINE)
    assert m, "в supervise.ps1 нет $HeartbeatPattern"
    pattern = re.compile(m.group(1))

    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, client, t = make(tmp_path)
    runner.tick()
    assert runner.heartbeat()
    record = next(r for r in caplog.records if r.getMessage().startswith("heartbeat:"))
    line = logging.Formatter(LOG_FORMAT).format(record)

    found = pattern.findall("2026-10-16 03:45:42,814 INFO oskar.shadow: tick 13: {}\r\n" + line + "\r\n")
    assert len(found) == 1
    stamp = datetime.strptime(found[0], "%Y-%m-%d %H:%M:%S")
    assert abs((datetime.fromtimestamp(record.created) - stamp).total_seconds()) < 1.5
    # строка тика (только при кандидатах) пульсом не считается
    assert not pattern.search("2026-10-16 03:45:42,814 INFO oskar.shadow: tick 13: {'scanned': 20}")
