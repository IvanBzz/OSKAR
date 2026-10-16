"""Управление oskard через локальный API. Только stdlib — venv-независим.

Блок I, 2026-10-16: SKILL.md обещал /positions и /pnl, которых в скрипте не было. Добавлены
positions (GET /positions), pnl [часы] (GET /pnl), mode [auto|confirm] (GET/POST /mode).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8321"
COMMANDS = {
    "status": ("GET", "/status"),
    "pending": ("GET", "/pending"),
    "positions": ("GET", "/positions"),
    "pnl": ("GET", "/pnl"),
    "mode": ("GET", "/mode"),
    "pause": ("POST", "/pause"),
    "resume": ("POST", "/resume"),
    "panic": ("POST", "/panic"),
    "panic-reset": ("POST", "/panic/reset"),
}
MODES = ("auto", "confirm")
USAGE = (f"usage: oskarctl.py [{'|'.join(COMMANDS)}]  "
         "| oskarctl.py mode [auto|confirm] | oskarctl.py pnl [hours, 0 = all]")


def _oskar_home() -> Path:
    """Корень репозитория OSKAR. Скилл живет копией в hermes/skills — относительный
    путь там неверен, поэтому деплой кладет рядом oskar_home.txt (setup_hermes.ps1)."""
    marker = Path(__file__).resolve().parent / "oskar_home.txt"
    try:
        return Path(marker.read_text(encoding="utf-8").strip())
    except OSError:
        return Path(__file__).resolve().parents[3]


def api_token() -> str:
    """Токен демона: env OSKAR_API_TOKEN или <OSKAR>/data/oskard.token (пишет oskard при старте)."""
    tok = os.getenv("OSKAR_API_TOKEN", "")
    if tok:
        return tok
    try:
        return (_oskar_home() / "data" / "oskard.token").read_text(encoding="ascii").strip()
    except OSError:
        return ""


# Демон на loopback: системный прокси из окружения (HTTP_PROXY) сюда не нужен и может
# не пропустить запрос к 127.0.0.1 — открываем без прокси (ревью блока D 2026-10-15)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request_for(args: list[str]) -> tuple[str, str] | None:
    """(метод, путь) для аргументов командной строки; None — неверный вызов."""
    if not args or args[0] not in COMMANDS:
        return None
    command, rest = args[0], args[1:]
    method, path = COMMANDS[command]
    if command == "mode" and rest:
        if len(rest) != 1 or rest[0] not in MODES:
            return None
        return "POST", f"/mode/{rest[0]}"
    if command == "pnl" and rest:
        try:
            hours = float(rest[0])
        except ValueError:
            return None
        if len(rest) != 1 or hours < 0:
            return None
        return method, f"{path}?hours={hours:g}"
    if rest:
        return None
    return method, path


def main() -> int:
    target = request_for(sys.argv[1:])
    if target is None:
        print(USAGE)
        return 2
    method, path = target
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Oskar-Token": api_token()})
    if method == "POST":
        req.data = b""
    try:
        with _OPENER.open(req, timeout=10) as resp:
            print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
            return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as e:
        print(f"OSKARD NOT RUNNING? connection failed: {e.reason}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
