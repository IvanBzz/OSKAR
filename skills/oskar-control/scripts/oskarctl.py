"""Управление oskard через локальный API. Только stdlib — venv-независим."""

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
    "pause": ("POST", "/pause"),
    "resume": ("POST", "/resume"),
    "panic": ("POST", "/panic"),
    "panic-reset": ("POST", "/panic/reset"),
}


def api_token() -> str:
    """Токен демона: env OSKAR_API_TOKEN или data/oskard.token (пишет oskard при старте)."""
    tok = os.getenv("OSKAR_API_TOKEN", "")
    if tok:
        return tok
    path = Path(__file__).resolve().parents[3] / "data" / "oskard.token"
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: oskarctl.py [{'|'.join(COMMANDS)}]")
        return 2
    method, path = COMMANDS[sys.argv[1]]
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Oskar-Token": api_token()})
    if method == "POST":
        req.data = b""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
