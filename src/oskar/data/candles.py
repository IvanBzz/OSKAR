"""Кеш закрытых свечей: к бирже — только когда закрылся новый бар (блок K, 2026-10-16).

Было: TTL per-timeframe (15m 30 с ... 1d 15 мин) — цикл делал ~65 запросов свечей в
минуту, хотя закрытые бары между закрытиями не меняются; символ с историей короче
limit перезапрашивался на каждом вызове. Стало: запись (symbol, timeframe) помнит
окно закрытых свечей, limit, с которым его грузили, и время закрытия следующего бара.
Запрос к бирже идет, только если:
- записи нет (первый вызов или invalidate);
- запрошен больший limit, чем загружен (история короче загруженного limit — вся, что есть);
- закрылся следующий бар: now >= ts_последнего + 2*tf + GRACE_SEC (ts — открытие:
  +tf — закрытие последнего закрытого, еще +tf — закрытие формировавшегося).
Иначе — срез последних limit баров из кеша: ровно то, что вернул бы прямой
fetch_closed_ohlcv(limit) в тот же момент (вне окна люфта; тест эквивалентности цикла
в tests/unit/test_candle_cache.py). Перезапрос — всегда с наибольшим загруженным limit:
скан (1h x 80) и цикл (1h x 260) не делят один бар на два запроса.

Биржа еще отдает старый бар (лаг биржи, наши часы впереди, пропуск бара у неликвида) или
окно короче прошлого (ответ неполный) — повтор не раньше STALE_RETRY_SEC с удвоением до
STALE_RETRY_MAX_SEC, а не на каждом вызове. ТФ без фиксированной длины (1M) и
нераспознанные — прежний TTL DEFAULT_TTL_SEC. Символ в черном списке клиента (ошибка
на любом ТФ) из кеша не отдается: get откажет, как отказал бы прямой запрос.
Ошибка запроса не кешируется и запись не трогает — следующий вызов повторит запрос.

Окно грузится целиком, since не используем: число запросов то же (вес запроса свечей
от since не зависит), а since на месте limit уже стоил бана (см. client.ExchangeLike).
Формирующуюся свечу отбрасывает client — в кеш она не попадает (look-ahead).

Часы инжектируемые: wall — epoch-секунды (сравнение с ts баров), clock — monotonic
(повторы и сводка в лог раз в STATS_LOG_SEC). Кеш живет в потоке runner'а; stats()
зовется из потока API — счетчики под замком. Отдается копия: мутации потребителя
не портят кеш.
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass

import pandas as pd

from oskar.data.client import MarketDataClient
from oskar.data.symbols import canonical

logger = logging.getLogger("oskar.data")

GRACE_SEC = 3.0  # люфт после закрытия бара: биржа успевает отдать закрытую свечу
STALE_RETRY_SEC = 15.0  # биржа отдала старый бар — первый повтор
STALE_RETRY_MAX_SEC = 300.0  # потолок удвоения: неликвид без новых баров — 1 запрос в 5 мин
DEFAULT_TTL_SEC = 60.0  # ТФ без фиксированной длины (1M) и нераспознанные
STATS_LOG_SEC = 1800.0  # сводка запросов/попаданий в лог раз в 30 мин

_TF_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
# причины запроса к бирже (ключи счетчиков fetch_<reason>)
FETCH_REASONS = ("cold", "limit", "bar_close", "stale", "ttl")


def timeframe_ms(timeframe: str) -> int | None:
    """'15m' -> 900000. None — у ТФ нет фиксированной длины ('1M' — месяц) или это мусор."""
    unit = _TF_UNIT_MS.get(timeframe[-1:])
    amount = timeframe[:-1]
    if unit is None or not amount.isdigit() or int(amount) <= 0:
        return None
    return int(amount) * unit


@dataclass
class _Entry:
    df: pd.DataFrame
    limit: int  # с каким limit грузили; len(df) < limit — истории меньше, это вся история
    refresh_at: float | None = None  # wall: закрылся следующий бар (+люфт) — пора за ним
    retry_at: float | None = None  # monotonic: повтор после старого бара / по TTL
    retry_reason: str = ""  # stale | ttl
    stale: int = 0  # ответов подряд без ожидаемого бара (степень удвоения)


class CandleCache:
    def __init__(
        self,
        client: MarketDataClient,
        *,
        clock=time.monotonic,
        wall=time.time,
        grace_sec: float = GRACE_SEC,
        log_every_sec: float = STATS_LOG_SEC,
    ):
        self._client = client
        self._clock = clock
        self._wall = wall
        self._grace = grace_sec
        self._log_every = log_every_sec
        self._store: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()  # только счетчики: сеть под замком не ходит
        self._totals: Counter = Counter()  # с запуска (для /status)
        self._window: Counter = Counter()  # с последней сводки в лог
        self._logged_at = clock()

    def get(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        key = (canonical(symbol), timeframe)
        self._maybe_log()
        entry = self._store.get(key)
        if entry is not None and self._blacklisted(key[0]):
            # клиент уже приговорил символ (ошибка на другом ТФ): кеш не прячет отказ,
            # который дал бы прямой запрос, — запись долой, fetch откажет без сети
            del self._store[key]
            entry = None
        reason = self._fetch_reason(entry, limit)
        if reason is None:
            self._count("hit")
            return entry.df.iloc[-limit:].reset_index(drop=True).copy()
        fetch_limit = limit if entry is None else max(limit, entry.limit)
        try:
            df = self._client.fetch_closed_ohlcv(key[0], timeframe, fetch_limit)
        except Exception:
            self._count("error")  # запись не трогаем: следующий вызов повторит запрос
            raise
        self._count(f"fetch_{reason}")
        self._store[key] = self._new_entry(df, fetch_limit, timeframe, entry)
        return df.iloc[-limit:].reset_index(drop=True).copy()

    def invalidate(self, symbol: str | None = None, timeframe: str | None = None) -> int:
        """Сброс записей при ошибке данных (битое окно, ручная починка): следующий get
        идет к бирже. Без аргументов — весь кеш; symbol и/или timeframe сужают.
        Возвращает число сброшенных записей."""
        sym = canonical(symbol) if symbol is not None else None
        keys = [
            k for k in self._store
            if (sym is None or k[0] == sym) and (timeframe is None or k[1] == timeframe)
        ]
        for k in keys:
            del self._store[k]
        if keys:
            self._count("invalidated", len(keys))
        return len(keys)

    def stats(self) -> dict:
        """Счетчики с запуска для /status: запросы к бирже по причинам, попадания, ошибки."""
        with self._lock:
            totals = dict(self._totals)
        return {**_summary(totals), "keys": len(self._store)}

    # ------------------------------------------------------------ internals

    def _blacklisted(self, symbol: str) -> bool:
        fn = getattr(self._client, "is_blacklisted", None)
        return bool(fn(symbol)) if callable(fn) else False

    def _fetch_reason(self, entry: _Entry | None, limit: int) -> str | None:
        """None — отдаем из кеша; иначе причина запроса к бирже."""
        if entry is None:
            return "cold"
        if limit > entry.limit:
            return "limit"
        if entry.retry_at is not None:
            return entry.retry_reason if self._clock() >= entry.retry_at else None
        return "bar_close" if self._wall() >= entry.refresh_at else None

    def _new_entry(self, df: pd.DataFrame, limit: int, timeframe: str, prev: _Entry | None) -> _Entry:
        tf_ms = timeframe_ms(timeframe)
        if tf_ms is None or df.empty:
            return _Entry(df, limit, retry_at=self._clock() + DEFAULT_TTL_SEC, retry_reason="ttl")
        refresh_at = (int(df["ts"].iloc[-1]) + 2 * tf_ms) / 1000 + self._grace
        # история не убывает: окно короче прошлого — ответ неполный, «вся история» ему не верим
        shrunk = prev is not None and len(df) < min(len(prev.df), limit)
        if self._wall() < refresh_at and not shrunk:
            return _Entry(df, limit, refresh_at=refresh_at)  # свежее окно: ждем закрытия бара
        # следующий бар уже должен был закрыться (или окно обрезано), а биржа его не отдала
        stale = (prev.stale if prev is not None else 0) + 1
        delay = min(STALE_RETRY_SEC * 2 ** (stale - 1), STALE_RETRY_MAX_SEC)
        return _Entry(df, limit, retry_at=self._clock() + delay, retry_reason="stale", stale=stale)

    def _count(self, what: str, n: int = 1) -> None:
        with self._lock:
            self._totals[what] += n
            self._window[what] += n

    def _maybe_log(self) -> None:
        now = self._clock()
        elapsed = now - self._logged_at
        if elapsed < self._log_every:
            return
        with self._lock:
            window = dict(self._window)
            self._window.clear()
        self._logged_at = now
        s = _summary(window)
        minutes = elapsed / 60
        reasons = ", ".join(f"{k} {v}" for k, v in s["by_reason"].items() if v) or "-"
        logger.info(
            "candle cache %.0f min: %d exchange requests (%.1f/min; %s), %d hits (%.0f%%), "
            "%d errors, %d invalidated, %d keys",
            minutes, s["requests"], s["requests"] / minutes if minutes else 0.0, reasons,
            s["hits"], 100.0 * s["hit_rate"], s["errors"], s["invalidated"], len(self._store),
        )


def _summary(counts: dict) -> dict:
    by_reason = {r: counts.get(f"fetch_{r}", 0) for r in FETCH_REASONS}
    requests = sum(by_reason.values())
    hits = counts.get("hit", 0)
    served = requests + hits
    return {
        "requests": requests,
        "hits": hits,
        "hit_rate": round(hits / served, 4) if served else 0.0,
        "errors": counts.get("error", 0),
        "invalidated": counts.get("invalidated", 0),
        "by_reason": by_reason,
    }
