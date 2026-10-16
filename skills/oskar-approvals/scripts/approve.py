"""Доставка решения Ивана демону oskard. Только stdlib.

Блок I, 2026-10-16: approve теперь ИСПОЛНЯЕТ (тот же исполнитель, что у кнопок карточки) и
возвращает фактический исход. Первая строка вывода — итог для пересказа Ивану:
ИСПОЛНЕНО / ВХОД УШЕЛ, НО ... / ИСХОД НЕИЗВЕСТЕН / НЕ ИСПОЛНЕНО; дальше — JSON демона.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8321"
ENTRY_TYPES = ("market", "limit")
# Исполнение — это биржа: плечо, вход, ожидание позиции и защита занимают секунды-десятки секунд
APPROVE_TIMEOUT_SEC = 90
OTHER_TIMEOUT_SEC = 10


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


def summarize(payload: dict) -> str:
    """Итог исполнения одной строкой — пересказывать Ивану ЕГО, а не обещание."""
    note = payload.get("note", "")
    kind = payload.get("kind", "")
    if payload.get("ok"):
        return f"ИСПОЛНЕНО: {note}"
    if payload.get("entry_sent"):
        return f"ВХОД УШЕЛ НА БИРЖУ, НО: {note} [{kind}] — повторно НЕ подтверждать"
    if payload.get("resolution") == "approved":
        return f"ИСХОД НЕИЗВЕСТЕН: {note} [{kind}] — проверить биржу, повторно НЕ подтверждать"
    return f"НЕ ИСПОЛНЕНО: {note} [{kind}] — ордер не ставился, карточка жива"


def main() -> int:
    args = sys.argv[1:]
    ok_shape = (len(args) == 2 and args[0] in ("approve", "reject")) or (
        len(args) == 3 and args[0] == "approve" and args[2] in ENTRY_TYPES)
    if not ok_shape:
        print("usage: approve.py approve <decision_id> [market|limit] | approve.py reject <decision_id>")
        return 2
    action, decision_id = args[0], args[1]
    url = f"{BASE}/decisions/{decision_id}/{action}"
    if len(args) == 3:
        url += f"?entry={args[2]}"
    req = urllib.request.Request(url, method="POST", data=b"", headers={"X-Oskar-Token": api_token()})
    timeout = APPROVE_TIMEOUT_SEC if action == "approve" else OTHER_TIMEOUT_SEC
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as e:
        print(f"OSKARD NOT RUNNING? connection failed: {e.reason}")
        return 1
    except TimeoutError:
        print("ИСХОД НЕИЗВЕСТЕН: демон не ответил за отведенное время — проверить /positions, "
              "повторно НЕ подтверждать")
        return 1
    if action != "approve":
        print(body)
        return 0
    try:
        payload = json.loads(body)
    except ValueError:
        print(body)
        return 1
    print(summarize(payload))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
