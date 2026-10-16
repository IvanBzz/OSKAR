"""Эпизоды counterfactuals: серия почти одинаковых гипотетических сделок одного сетапа.

Ночные проверки скептиков 2026-10-16: кулдаун символа 30 мин при горизонте 12 ч —
один сетап дает до 10 строк подряд (199 сделок = 115 эпизодов, ICC 0.19). Статистика
по строкам считает их независимыми и занижает CI, поэтому у строки есть episode_id:
символ + направление, новая серия — если от НАЧАЛА текущей прошло больше горизонта.

Только stdlib: модуль зовет и миграция журнала (journal/db.py), и цикл.
"""

import json
import sqlite3
from datetime import UTC, datetime

# Горизонт CF в барах сигнального ТФ (48 x 15м = 12 ч): окно исхода резолвера и длина эпизода
HORIZON_BARS = 48

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
DEFAULT_TIMEFRAME = "15m"
_SEP = "|"


def iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(str(iso))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def horizon_ms(timeframe: str | None) -> int:
    return HORIZON_BARS * TIMEFRAME_MS.get(timeframe or DEFAULT_TIMEFRAME, TIMEFRAME_MS[DEFAULT_TIMEFRAME])


def make_episode_id(symbol: str, direction: str, start_ms: int) -> str:
    """'SOL/USDT:USDT|long|2026-10-16T11:00:05Z' — начало серии в id (секунды UTC)."""
    return _SEP.join((symbol, direction, ms_to_iso(start_ms)))


def episode_start_ms(episode_id: str) -> int:
    return iso_to_ms(episode_id.rsplit(_SEP, 1)[1])


def next_episode(prev_id: str | None, symbol: str, direction: str, ts_ms: int, span_ms: int) -> str:
    """Строка продолжает серию prev_id, если пришла не раньше ее начала и не позже начала + горизонт."""
    if prev_id:
        start = episode_start_ms(prev_id)
        if 0 <= ts_ms - start <= span_ms:
            return prev_id
    return make_episode_id(symbol, direction, ts_ms)


def assign_episode(
    conn: sqlite3.Connection, symbol: str, direction: str, ts_ms: int, timeframe: str | None
) -> str:
    """episode_id новой строки: последняя серия символа+направления (по началу) или новая."""
    prefix = f"{symbol}{_SEP}{direction}{_SEP}"
    row = conn.execute(
        "SELECT episode_id FROM counterfactuals WHERE substr(episode_id, 1, ?) = ? "
        "ORDER BY episode_id DESC LIMIT 1",  # ISO одной ширины: лексикографически = по времени
        (len(prefix), prefix),
    ).fetchone()
    return next_episode(row[0] if row else None, symbol, direction, ts_ms, horizon_ms(timeframe))


def _key(candidate_json: str) -> tuple[str, str, int, str | None] | None:
    try:
        c = json.loads(candidate_json)
        symbol, direction, ts = c["symbol"], c["direction"], c["ts"]
        if not symbol or direction not in ("long", "short"):
            return None
        return str(symbol), str(direction), iso_to_ms(ts), c.get("timeframe")
    except (TypeError, ValueError, KeyError):
        return None


def backfill_episodes(conn: sqlite3.Connection) -> int:
    """Миграция v5: episode_id строкам без него — тем же правилом, что у живой записи,
    в порядке времени кандидата. Исход и цены строк не трогаются. Без symbol/direction/ts — NULL."""
    keyed = []
    for r in conn.execute("SELECT id, candidate_json FROM counterfactuals WHERE episode_id IS NULL"):
        k = _key(r[1])
        if k is not None:
            keyed.append((k[0], k[1], k[2], r[0], k[3]))
    keyed.sort()
    current: dict[tuple[str, str], str] = {}
    updates = []
    for symbol, direction, ts_ms, row_id, tf in keyed:
        ep = next_episode(current.get((symbol, direction)), symbol, direction, ts_ms, horizon_ms(tf))
        current[(symbol, direction)] = ep
        updates.append((ep, row_id))
    conn.executemany("UPDATE counterfactuals SET episode_id=? WHERE id=?", updates)
    return len(updates)
