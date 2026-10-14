"""Доставка решения Ивана демону oskard. Только stdlib."""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8321"


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


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("approve", "reject"):
        print("usage: approve.py [approve|reject] <decision_id>")
        return 2
    action, decision_id = sys.argv[1], sys.argv[2]
    req = urllib.request.Request(
        f"{BASE}/decisions/{decision_id}/{action}", method="POST", data=b"",
        headers={"X-Oskar-Token": api_token()},
    )
    try:
        with _OPENER.open(req, timeout=10) as resp:
            print(resp.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as e:
        print(f"OSKARD NOT RUNNING? connection failed: {e.reason}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
