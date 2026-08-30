"""Доставка решения Ивана демону oskard. Только stdlib."""

import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8321"


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("approve", "reject"):
        print("usage: approve.py [approve|reject] <decision_id>")
        return 2
    action, decision_id = sys.argv[1], sys.argv[2]
    req = urllib.request.Request(
        f"{BASE}/decisions/{decision_id}/{action}", method="POST", data=b""
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
