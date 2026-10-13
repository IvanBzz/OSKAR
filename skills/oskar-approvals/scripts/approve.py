"""Доставка решения Ивана демону oskard. Только stdlib."""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8321"


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
    if len(sys.argv) != 3 or sys.argv[1] not in ("approve", "reject"):
        print("usage: approve.py [approve|reject] <decision_id>")
        return 2
    action, decision_id = sys.argv[1], sys.argv[2]
    req = urllib.request.Request(
        f"{BASE}/decisions/{decision_id}/{action}", method="POST", data=b"",
        headers={"X-Oskar-Token": api_token()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
