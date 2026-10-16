"""Кеш закрытых свечей: к бирже — только когда закрылся новый бар (блок K, 2026-10-16).

Было: TTL per-timeframe (15m 30 с ... 1d 15 мин) — цикл делал ~65 запросов свечей в
минуту, хотя закрытые бары между закрытиями не меняются; символ с историей короче
limit перезапрашивался на каждом вызове. Стало: запись (symbol, timeframe) помнит
окно закрытых свечей, limit, с которым его грузили, и время закрытия следующего бара.
Запрос к бирже идет, только если:
- записи нет (первый вызов или invalidate);
- запрошен больший limit, чем загружен;
- закрылся следующий бар: now >= ts_последнего + 2*tf + GRACE_SEC (ts — открытие:
  +tf — закрытие последнего закрытого, еще +tf — закрытие формировавшегося);
- подошел срок перепроверки (ниже).
Иначе — срез последних limit баров из кеша: ровно то, что вернул бы прямой
fetch_closed_ohlcv(limit) в тот же момент (вне окна люфта и перепроверок; тест
эквивалентности цикла в tests/unit/test_candle_cache.py). Перезапрос — всегда с наибольшим загруженным limit:
скан (1h x 80) и цикл (1h x 260) не делят один бар на два запроса.

Перепроверка — через STALE_RETRY_SEC с удвоением до STALE_RETRY_MAX_SEC, но не позже
закрытия следующего бара (+люфт): иначе неликвид без бара видел новый бар через минуты
после прямого запроса (ревью блока K, 2026-10-16). Причины:
- stale — биржа еще отдает старый бар (лаг биржи, пропуск бара у неликвида);
- verify — окну не верим как «всей истории»: короче limit без подтверждения (два ответа
  подряд начинаются с одного бара — начало листинга стабильно) или короче уже виденного
  (длина не убывает: обрезок ее не понижает). Ревью: холодный обрезок жил «всей
  историей» до закрытия бара (на 4h — часы), второй обрезок подряд проходил сверку;
- confirm — биржа поправила уже закрытые бары (сверка пересечения окон на каждом
  перезапросе: счетчик revised, предупреждение в лог): у ключа каждый новый бар
  перечитывается еще раз, а не живет первым снимком весь бар.
ТФ без фиксированной длины (1M) и нераспознанные — прежний TTL DEFAULT_TTL_SEC.

Границы баров — по часам биржи: смещение по server time при старте и раз в
CLOCK_SYNC_SEC (client.server_time_offset), больше люфта — предупреждение в лог.
Отстающие часы Windows задерживали каждое обновление на величину отставания, и ни один
счетчик этого не показывал (ревью блока K).

Символ в черном списке клиента (ошибка на любом ТФ) из кеша не отдается: get откажет,
как отказал бы прямой запрос. Ошибка запроса не кешируется и запись не трогает —
следующий вызов повторит запрос. Отказ клиента без сети (черный список, cooldown) —
счетчик refused, сбой на бирже — errors. Потребитель, упавший на окне из кеша (битые
данные), сбрасывает запись через drop_on_data_error — следующий get идет к бирже.

Окно грузится целиком, since не используем: число запросов то же (вес запроса свечей
от since не зависит), а since на месте limit уже стоил бана (см. client.ExchangeLike).
Формирующуюся свечу отбрасывает client — в кеш она не попадает (look-ahead).

Часы инжектируемые: wall — epoch-секунды (сравнение с ts баров), clock — monotonic
(перепроверки, синхронизация часов, сводка в лог раз в STATS_LOG_SEC). Кеш живет в
потоке runner'а; stats() зовется из потока API — счетчики под замком. Отдается копия:
мутации потребителя не портят кеш.
"""

import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
import pandas as pd

from oskar.data.client import MarketDataClient
from oskar.data.symbols import canonical
from oskar.detect.indicators import InsufficientHistory

logger = logging.getLogger("oskar.data")

GRACE_SEC = 3.0  # люфт после закрытия бара: биржа успевает отдать закрытую свечу
STALE_RETRY_SEC = 15.0  # первая перепроверка (старый бар, подозрительное окно, правки баров)
STALE_RETRY_MAX_SEC = 300.0  # потолок удвоения: неликвид без новых баров — 1 запрос в 5 мин
DEFAULT_TTL_SEC = 60.0  # ТФ без фиксированной длины (1M) и нераспознанные
STATS_LOG_SEC = 1800.0  # сводка запросов/попаданий в лог раз в 30 мин
CLOCK_SYNC_SEC = 1800.0  # смещение часов к бирже: при старте и раз в 30 мин
CLOCK_SYNC_RETRY_SEC = 300.0  # замер не удался — повтор через 5 мин

_TF_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_OHLCV = ["open", "high", "low", "close", "volume"]
# причины запроса к бирже (ключи счетчиков fetch_<reason>)
FETCH_REASONS = ("cold", "limit", "bar_close", "stale", "verify", "confirm", "ttl")


def timeframe_ms(timeframe: str) -> int | None:
    """'15m' -> 900000. None — у ТФ нет фиксированной длины ('1M' — месяц) или это мусор."""
    unit = _TF_UNIT_MS.get(timeframe[-1:])
    amount = timeframe[:-1]
    if unit is None or not amount.isdigit() or int(amount) <= 0:
        return None
    return int(amount) * unit


@contextmanager
def drop_on_data_error(cache, symbol: str, timeframe: str):
    """Обработка окна из кеша упала (битые данные) — запись (symbol, timeframe) долой:
    следующий get идет к бирже, а не отдает то же окно до закрытия бара. Короткая
    история (InsufficientHistory) — свойство символа, запись остается. Исключение
    пробрасывается как было. Кеш без invalidate (фейки, прямой запрос) — только проброс.
    Ревью блока K, 2026-10-16: invalidate не звал никто — битое окно уходило рестартом."""
    try:
        yield
    except InsufficientHistory:
        raise
    except Exception:
        fn = getattr(cache, "invalidate", None)
        if callable(fn):
            fn(symbol, timeframe)
        raise


@dataclass
class _Entry:
    df: pd.DataFrame
    limit: int  # с каким limit грузили; len(df) < limit — короткая история (или обрезок)
    seen_len: int = 0  # наибольшая длина окна ключа: история не убывает, обрезок не понижает
    refresh_at: float | None = None  # часы биржи: закрылся следующий бар (+люфт) — пора за ним
    retry_at: float | None = None  # monotonic: перепроверка / TTL
    retry_reason: str = ""  # stale | verify | confirm | ttl
    retries: int = 0  # перепроверок подряд (степень удвоения)
    confirm: bool = False  # биржа правила закрытые бары ключа: новый бар перечитываем


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
        self._offset: float | None = None  # часы биржи минус наши, сек (None — не замерено)
        self._sync_at: float | None = None  # monotonic: следующий замер (None — при первом get)
        self._revised_keys: set[tuple[str, str]] = set()  # о правках ключа уже предупредили

    def get(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        key = (canonical(symbol), timeframe)
        self._maybe_sync_clock()
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
        refused = self._refuses(key[0])  # клиент откажет без сети: это не сбой биржи
        try:
            df = self._client.fetch_closed_ohlcv(key[0], timeframe, fetch_limit)
        except Exception:
            self._count("refused" if refused else "error")  # запись не трогаем: повтор на следующем
            raise
        self._count(f"fetch_{reason}")
        self._store[key] = self._new_entry(key, df, fetch_limit, entry)
        return df.iloc[-limit:].reset_index(drop=True).copy()

    def invalidate(self, symbol: str | None = None, timeframe: str | None = None) -> int:
        """Сброс записей при ошибке данных (битое окно, ручная починка): следующий get
        идет к бирже. Без аргументов — весь кеш; symbol и/или timeframe сужают.
        Возвращает число сброшенных записей. В цикле зовется через drop_on_data_error."""
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
        """Счетчики с запуска для /status: запросы к бирже по причинам, попадания, ошибки
        биржи, отказы без сети, правки закрытых баров, смещение часов к бирже."""
        with self._lock:
            totals = dict(self._totals)
        return {**_summary(totals), "keys": len(self._store), "clock_offset_sec": self._offset_sec()}

    # ------------------------------------------------------------ internals

    def _now(self) -> float:
        """Часы биржи: наш wall + замеренное смещение (0, пока не замерено)."""
        return self._wall() + (self._offset or 0.0)

    def _offset_sec(self) -> float | None:
        return None if self._offset is None else round(self._offset, 3)

    def _maybe_sync_clock(self) -> None:
        now = self._clock()
        if self._sync_at is not None and now < self._sync_at:
            return
        fn = getattr(self._client, "server_time_offset", None)
        offset = fn() if callable(fn) else None
        self._sync_at = now + (CLOCK_SYNC_SEC if offset is not None else CLOCK_SYNC_RETRY_SEC)
        if offset is None:
            return
        self._offset = offset
        if abs(offset) > self._grace:
            logger.warning(
                "clock offset to exchange %+.1fs (exchange %s): bar boundaries follow exchange time",
                offset, "ahead" if offset > 0 else "behind",
            )

    def _blacklisted(self, symbol: str) -> bool:
        fn = getattr(self._client, "is_blacklisted", None)
        return bool(fn(symbol)) if callable(fn) else False

    def _refuses(self, symbol: str) -> bool:
        fn = getattr(self._client, "cooldown_remaining", None)
        return (callable(fn) and fn() > 0) or self._blacklisted(symbol)

    def _fetch_reason(self, entry: _Entry | None, limit: int) -> str | None:
        """None — отдаем из кеша; иначе причина запроса к бирже."""
        if entry is None:
            return "cold"
        if limit > entry.limit:
            return "limit"
        if entry.retry_at is not None:
            return entry.retry_reason if self._clock() >= entry.retry_at else None
        return "bar_close" if self._now() >= entry.refresh_at else None

    def _new_entry(self, key, df: pd.DataFrame, limit: int, prev: _Entry | None) -> _Entry:
        tf_ms = timeframe_ms(key[1])
        if tf_ms is None or df.empty:
            return _Entry(df, limit, retry_at=self._clock() + DEFAULT_TTL_SEC, retry_reason="ttl")
        seen = max(len(df), prev.seen_len if prev is not None else 0)
        confirm = prev is not None and prev.confirm
        if prev is not None and self._revised(key, prev.df, df):
            confirm = True
        entry = _Entry(df, limit, seen_len=seen, confirm=confirm)
        last = int(df["ts"].iloc[-1])
        if self._suspect(df, limit, prev):
            return self._retry(entry, "verify", prev, last, tf_ms)
        refresh_at = (last + 2 * tf_ms) / 1000 + self._grace
        if self._now() >= refresh_at:
            # следующий бар уже должен был закрыться, а биржа его не отдала
            return self._retry(entry, "stale", prev, last, tf_ms)
        entry.refresh_at = refresh_at  # свежее окно: ждем закрытия бара
        new_bar = prev is None or prev.df.empty or last > int(prev.df["ts"].iloc[-1])
        if confirm and new_bar:
            # биржа правит закрытые бары: новый бар перечитать еще раз, не жить первым снимком
            entry.retry_at = self._clock() + min(STALE_RETRY_SEC, self._to_next_close(last, tf_ms))
            entry.retry_reason = "confirm"
        return entry

    @staticmethod
    def _suspect(df: pd.DataFrame, limit: int, prev: _Entry | None) -> bool:
        """Окну не верим как «всей истории»: короче уже виденного (ответ неполный) или
        короче limit, а прошлый ответ не был коротким с тем же первым баром."""
        if prev is not None and len(df) < min(prev.seen_len, limit):
            return True
        if len(df) >= limit:
            return False
        return not (
            prev is not None and not prev.df.empty and len(prev.df) < prev.limit
            and int(prev.df["ts"].iloc[0]) == int(df["ts"].iloc[0])
        )

    def _retry(self, entry: _Entry, reason: str, prev: _Entry | None, last: int, tf_ms: int) -> _Entry:
        entry.retries = (prev.retries if prev is not None else 0) + 1
        delay = min(STALE_RETRY_SEC * 2 ** (entry.retries - 1), STALE_RETRY_MAX_SEC)
        entry.retry_at = self._clock() + min(delay, self._to_next_close(last, tf_ms))
        entry.retry_reason = reason
        return entry

    def _to_next_close(self, last_ts: int, tf_ms: int) -> float:
        """Секунд до закрытия формирующегося бара (+люфт) по часам биржи. Сетка — от ts
        последнего бара: так верно и для 1w (бары с понедельника, а не с эпохи)."""
        now_ms = self._now() * 1000
        k = max(0, int((now_ms - last_ts) // tf_ms))
        return (last_ts + (k + 1) * tf_ms - now_ms) / 1000 + self._grace

    def _revised(self, key, old: pd.DataFrame, new: pd.DataFrame) -> bool:
        """Биржа поправила бары, которые уже были закрыты в прошлом ответе (сверка по ts)."""
        both = old.merge(new, on="ts", suffixes=("_old", ""))
        if both.empty:
            return False
        a = both[[f"{c}_old" for c in _OHLCV]].to_numpy(dtype=float)
        b = both[_OHLCV].to_numpy(dtype=float)
        rows = (~((a == b) | (np.isnan(a) & np.isnan(b)))).any(axis=1)
        if not rows.any():
            return False
        self._count("revised")
        if key not in self._revised_keys:  # дальше — только счетчик в сводке
            self._revised_keys.add(key)
            i = int(np.flatnonzero(rows)[-1])
            logger.warning(
                "candle cache: exchange revised %d closed bar(s) of %s %s after caching "
                "(ts %d close %r -> %r, volume %r -> %r); new bars of this key are re-read "
                "after %.0fs",
                int(rows.sum()), key[0], key[1], int(both["ts"].iloc[i]), a[i][3], b[i][3],
                a[i][4], b[i][4], STALE_RETRY_SEC,
            )
        return True

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
        offset = self._offset_sec()
        logger.info(
            "candle cache %.0f min: %d exchange requests (%.1f/min; %s), %d hits (%.0f%%), "
            "%d errors, %d refused, %d revised, %d invalidated, %d keys, clock offset %s",
            minutes, s["requests"], s["requests"] / minutes if minutes else 0.0, reasons,
            s["hits"], 100.0 * s["hit_rate"], s["errors"], s["refused"], s["revised"],
            s["invalidated"], len(self._store), "?" if offset is None else f"{offset:+.1f}s",
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
        "errors": counts.get("error", 0),  # сбой на бирже (сеть, ответ с ошибкой)
        "refused": counts.get("refused", 0),  # отказ клиента без сети: черный список, cooldown
        "revised": counts.get("revised", 0),  # ответов, где биржа поправила закрытые бары
        "invalidated": counts.get("invalidated", 0),
        "by_reason": by_reason,
    }
