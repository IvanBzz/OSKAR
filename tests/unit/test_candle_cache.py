"""Кеш свечей по закрытию бара (блок K, 2026-10-16): к бирже — только когда закрылся
новый бар; формирующаяся свеча не попадает; цикл с кешем видит ровно те же данные и
принимает те же решения, что без кеша.

Ревью блока K (2026-10-16): обрезанный ответ не становится «всей историей» (P2-1),
перепроверка не перешагивает закрытие следующего бара (P2-2), правки закрытых баров
биржей и смещение часов видны и учтены (P2-3), битое окно сбрасывается потребителем
(P2-4), отказы без сети — отдельный счетчик refused (P2-5)."""

import json
import logging
from collections import Counter
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from oskar.core.cycle import CycleConfig, ShadowCycle
from oskar.core.state import RuntimeState
from oskar.data.candles import (
    CLOCK_SYNC_RETRY_SEC,
    CLOCK_SYNC_SEC,
    DEFAULT_TTL_SEC,
    GRACE_SEC,
    STALE_RETRY_MAX_SEC,
    STALE_RETRY_SEC,
    STATS_LOG_SEC,
    CandleCache,
    timeframe_ms,
)
from oskar.data.client import ExchangeCooldown, MarketDataClient
from oskar.data.scanner import quick_score_one, scan
from oskar.detect.indicators import InsufficientHistory
from oskar.detect.smc import SmcConfig
from oskar.journal.db import connect
from oskar.risk.ladder import Caps, ProfileLimits

SYM = "SOL/USDT:USDT"
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1M": 30 * 86_400_000}
T0_MS = 1_699_999_200_000  # граница часа: 15m и 1h бары выровнены
CAPS = Caps(
    risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
    confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.3, sl_dist_pct_max=6.0, max_leverage=8)


def series(n: int, step_ms: int, start_ms: int, seed: int, *,
           fast: float = 0.0, noise: float = 0.35, drift: float = 0.01) -> pd.DataFrame:
    """Свинги (синусы + дрейф + шум): SMC находит структуру, цикл дает кандидатов;
    fast/noise — быстрые качели, чтобы исходы дорешивались за часы теста."""
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    close = (110 + 6 * np.sin(i / 9) + 3 * np.sin(i / 23) + fast * np.sin(i / 2.5)
             + drift * i + rng.normal(0, noise, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    return pd.DataFrame({
        "ts": start_ms + i * step_ms, "open": open_,
        "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
        "close": close, "volume": np.abs(rng.normal(1000, 300, n)) + 1,
    })


class ClockedExchange:
    """Биржа на фейковых часах: видны бары с открытием <= now - lag, последний —
    ФОРМИРУЮЩИЙСЯ, с частичными значениями (меняются со временем). lag — биржа
    отдает закрытый бар с опозданием. finalize_sec — биржа дописывает объем закрытого
    бара еще finalize_sec после закрытия (до того отдает 70%)."""

    def __init__(self, data: dict[str, pd.DataFrame], wall, lag_sec: float = 0.0,
                 finalize_sec: float = 0.0):
        self._data = data
        self._wall = wall
        self.lag_sec = lag_sec
        self.finalize_sec = finalize_sec
        self.calls: list[tuple[str, str, int]] = []

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        assert since is None and limit is not None  # limit — keyword, since не шлем
        self.calls.append((symbol, timeframe, limit))
        df = self._data[timeframe]
        now_ms = (self._wall() - self.lag_sec) * 1000
        rows = [
            [int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
            for r in df[df["ts"] <= now_ms].itertuples()
        ]
        ts, o, _h, _l, c, v = rows[-1]
        frac = (now_ms - ts) / TF_MS[timeframe]
        part = o + (c - o) * frac
        rows[-1] = [ts, o, max(o, part) + 0.01, min(o, part) - 0.01, part, v * frac]
        for r in rows[:-1]:  # закрыт, но объем еще дописывается
            if r[0] + TF_MS[timeframe] > now_ms - self.finalize_sec * 1000:
                r[5] *= 0.7
        return rows[-limit:]

    def load_markets(self, reload=False):
        return {}


def make_world(lag_sec: float = 0.0, n15: int = 420, n1h: int = 110, *,
               finalize_sec: float = 0.0, skew_sec: float = 0.0, server_time: bool = False):
    """15m: 400 бар истории до T0 (окно 300 полное); 1h: 100 бар (короче limit 261 —
    «вся история»). Часы биржи стартуют на T0 + 7 с; наши wall отстают на skew_sec.
    server_time — у биржи есть fetch_time (часы биржи, счетчик вызовов time_calls)."""
    wall = [T0_MS / 1000 + 7.0 - skew_sec]
    data = {
        "15m": series(n15, TF_MS["15m"], T0_MS - 400 * TF_MS["15m"], seed=11,
                      fast=1.0, noise=1.0, drift=0.0),
        "1h": series(n1h, TF_MS["1h"], T0_MS - 100 * TF_MS["1h"], seed=5),
        "4h": series(80, TF_MS["4h"], T0_MS - 70 * TF_MS["4h"], seed=8),
        "1d": series(40, TF_MS["1d"], T0_MS - 30 * TF_MS["1d"], seed=9),
        "1M": series(30, TF_MS["1M"], T0_MS - 20 * TF_MS["1M"], seed=7),
    }
    ex = ClockedExchange(data, lambda: wall[0] + skew_sec, lag_sec=lag_sec, finalize_sec=finalize_sec)
    if server_time:
        ex.time_calls = 0

        def fetch_time():
            ex.time_calls += 1
            return int((wall[0] + skew_sec) * 1000)

        ex.fetch_time = fetch_time
    client = MarketDataClient(ex, clock=lambda: wall[0], wall=lambda: wall[0], sleep=lambda s: None)
    cache = CandleCache(client, clock=lambda: wall[0], wall=lambda: wall[0])
    return wall, ex, client, cache


def forming_open_ms(wall_sec: float, tf: str) -> int:
    return int(wall_sec * 1000) // TF_MS[tf] * TF_MS[tf]


# ------------------------------------------------------------------ один бар


def test_one_request_per_bar_new_request_after_close():
    wall, ex, _, cache = make_world()
    for dt in (0, 30, 300, 880):  # много вызовов внутри одного бара
        wall[0] = T0_MS / 1000 + 7 + dt
        cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 1

    close = (T0_MS + TF_MS["15m"]) / 1000  # формирующийся бар закрылся
    # люфт не истек — еще кеш: отставание от прямого запроса <= люфта, вперед — никогда
    wall[0] = close + GRACE_SEC - 0.5
    assert int(cache.get(SYM, "15m", 300)["ts"].iloc[-1]) == T0_MS - TF_MS["15m"]
    assert len(ex.calls) == 1
    wall[0] = close + GRACE_SEC
    df = cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 2
    assert int(df["ts"].iloc[-1]) == T0_MS  # новый закрытый бар
    wall[0] = close + 600
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 2  # и снова кеш до следующего закрытия


def test_forming_candle_never_reaches_cache():
    wall, ex, client, cache = make_world()
    final = ex._data["15m"].set_index("ts")
    for dt in (0, 100, 450, 899):
        wall[0] = T0_MS / 1000 + dt + 0.5
        df = cache.get(SYM, "15m", 300)
        forming = forming_open_ms(wall[0], "15m")
        assert int(df["ts"].iloc[-1]) == forming - TF_MS["15m"]  # последний — закрытый
        assert (df["ts"] < forming).all()
        last = df.iloc[-1]
        assert last["close"] == final.loc[int(last["ts"]), "close"]  # окончательные значения
    # и прямой запрос в тот же момент отдает то же самое
    pd.testing.assert_frame_equal(cache.get(SYM, "15m", 300), client.fetch_closed_ohlcv(SYM, "15m", 300))


def test_smaller_limit_from_cache_bigger_limit_refetches():
    wall, ex, client, cache = make_world()
    small = cache.get(SYM, "15m", 80)
    assert ex.calls == [(SYM, "15m", 81)]
    big = cache.get(SYM, "15m", 300)  # больше загруженного — к бирже
    assert ex.calls[-1] == (SYM, "15m", 301) and len(big) == 300
    pd.testing.assert_frame_equal(small, big.iloc[-80:].reset_index(drop=True))
    again = cache.get(SYM, "15m", 80)  # меньший — срез, без запроса
    assert len(ex.calls) == 2
    pd.testing.assert_frame_equal(again, client.fetch_closed_ohlcv(SYM, "15m", 80))

    wall[0] = (T0_MS + TF_MS["15m"]) / 1000 + 10  # бар закрылся: малый запрос грузит
    cache.get(SYM, "15m", 80)  # наибольшее окно — следующий большой уже из кеша
    assert ex.calls[-1] == (SYM, "15m", 301)
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 4  # 3 своих + 1 прямой выше


def test_short_history_is_complete_history():
    """Истории меньше limit (молодой символ) — это вся история: не перезапрашиваем на
    каждом вызове (старый кеш промахивался всегда, когда len < limit). Но с первого
    ответа ей не верим (ревью P2-1): одна перепроверка через STALE_RETRY_SEC, второй
    ответ с тем же первым баром — история подтверждена."""
    wall, ex, _, cache = make_world()
    df = cache.get(SYM, "1h", 260)
    assert len(df) == 100  # 101 видимый бар минус формирующийся
    for _ in range(5):
        cache.get(SYM, "1h", 260)
        cache.get(SYM, "1h", 80)
    assert len(ex.calls) == 1
    wall[0] += STALE_RETRY_SEC
    assert len(cache.get(SYM, "1h", 260)) == 100
    assert len(ex.calls) == 2  # перепроверка: тот же первый бар — вся история
    for _ in range(20):
        wall[0] += 60
        cache.get(SYM, "1h", 260)
    assert len(ex.calls) == 2  # подтверждено: до закрытия бара из кеша
    wall[0] = (T0_MS + TF_MS["1h"]) / 1000 + GRACE_SEC
    assert len(cache.get(SYM, "1h", 260)) == 101  # бар закрылся: история выросла на бар
    wall[0] += 600
    cache.get(SYM, "1h", 260)
    assert len(ex.calls) == 3  # начало то же — без новой перепроверки
    assert cache.stats()["by_reason"] == {
        "cold": 1, "limit": 0, "bar_close": 1, "stale": 0, "verify": 1, "confirm": 0, "ttl": 0,
    }


def test_short_answer_to_bigger_limit_is_verified_once():
    """Скан (1h x 80) загрузил полное окно, цикл просит 260 — истории всего 100: короткий
    ответ на limit-запрос тоже перепроверяется один раз, потом — вся история."""
    wall, ex, _, cache = make_world()
    assert len(cache.get(SYM, "1h", 80)) == 80
    assert len(cache.get(SYM, "1h", 260)) == 100
    wall[0] += STALE_RETRY_SEC
    cache.get(SYM, "1h", 260)
    wall[0] += 600
    cache.get(SYM, "1h", 260)
    cache.get(SYM, "1h", 80)
    by = cache.stats()["by_reason"]
    assert (by["cold"], by["limit"], by["verify"], len(ex.calls)) == (1, 1, 1, 3)


def test_symbol_formats_share_entry():
    wall, ex, _, cache = make_world()
    cache.get("SOL/USDT:USDT", "15m", 100)
    cache.get("SOL-USDT", "15m", 100)
    cache.get("SOLUSDT", "15m", 100)
    assert len(ex.calls) == 1


# ------------------------------------------------------------- лаг биржи и TTL


def test_stale_exchange_retried_with_bounded_backoff():
    """Биржа отдает закрытый бар с опозданием 40 с: повтор через 15, затем 30 с — не
    на каждом вызове; как только бар пришел — снова ждем закрытия."""
    wall, ex, _, cache = make_world(lag_sec=40.0)
    wall[0] = T0_MS / 1000 + 60  # с лагом 40 с бар T0-15m уже отдан
    cache.get(SYM, "15m", 300)
    close = (T0_MS + TF_MS["15m"]) / 1000
    wall[0] = close + GRACE_SEC  # пора за новым баром, а биржа его еще не отдает
    df = cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 2 and int(df["ts"].iloc[-1]) == T0_MS - TF_MS["15m"]
    wall[0] += STALE_RETRY_SEC - 1
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 2  # повтор не раньше 15 с
    wall[0] += 1
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 3  # все еще старый (лаг 40 с) -> следующий через 30 с
    wall[0] += 2 * STALE_RETRY_SEC - 1
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 3
    wall[0] += 1
    df = cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 4 and int(df["ts"].iloc[-1]) == T0_MS  # бар пришел
    wall[0] += 300
    cache.get(SYM, "15m", 300)
    assert len(ex.calls) == 4  # свежий: ждем следующего закрытия


def test_stale_backoff_is_capped():
    """Данные стоят (биржа отстает на 2 дня): не чаще раза в STALE_RETRY_MAX_SEC."""
    wall, ex, _, cache = make_world(lag_sec=2 * 86_400)
    cache.get(SYM, "15m", 300)
    times = []
    for _ in range(4 * 3600 // 5):  # 4 часа шагом 5 с
        wall[0] += 5
        n = len(ex.calls)
        cache.get(SYM, "15m", 300)
        if len(ex.calls) > n:
            times.append(wall[0])
    gaps = np.diff(times)
    assert gaps.max() == STALE_RETRY_MAX_SEC
    assert len(times) < 4 * 3600 / STALE_RETRY_MAX_SEC + 6


def test_timeframe_without_fixed_length_uses_ttl():
    assert timeframe_ms("15m") == 900_000 and timeframe_ms("1w") == 604_800_000
    assert timeframe_ms("1M") is None and timeframe_ms("junk") is None and timeframe_ms("0h") is None
    wall, ex, _, cache = make_world()
    cache.get(SYM, "1M", 10)
    wall[0] += DEFAULT_TTL_SEC - 1
    cache.get(SYM, "1M", 10)
    assert len(ex.calls) == 1
    wall[0] += 1
    cache.get(SYM, "1M", 10)
    assert len(ex.calls) == 2


def test_shrunk_window_is_not_trusted():
    """Окно короче прошлого — ответ неполный (история не убывает): повтор через 15 с,
    а не «вся история» до следующего бара."""
    wall, ex, _, cache = make_world()
    cache.get(SYM, "15m", 300)
    wall[0] = (T0_MS + TF_MS["15m"]) / 1000 + 5
    real = ex.fetch_ohlcv
    ex.fetch_ohlcv = lambda *a, **k: real(*a, **k)[-50:]  # биржа отдала обрезок
    assert len(cache.get(SYM, "15m", 300)) == 49
    ex.fetch_ohlcv = real
    wall[0] += STALE_RETRY_SEC
    assert len(cache.get(SYM, "15m", 300)) == 300


def truncate(ex, rows: int):
    """Биржа отдает только последние rows строк (неполный ответ); вернуть — restore()."""
    real = ex.fetch_ohlcv
    ex.fetch_ohlcv = lambda *a, **k: real(*a, **k)[-rows:]
    return lambda: setattr(ex, "fetch_ohlcv", real)


def test_second_truncated_answer_in_a_row_is_not_trusted():
    """Ревью P2-1, случай 2: обрезок сохранялся в записи и становился эталоном длины —
    второй обрезок подряд проходил сверку, и 15m жил 49 барами против 300 у прямого
    запроса до следующего закрытия. Ожидаемая длина не убывает: второй обрезок —
    снова перепроверка (30 с), а не «вся история»."""
    wall, ex, client, cache = make_world()
    cache.get(SYM, "15m", 300)
    wall[0] = (T0_MS + TF_MS["15m"]) / 1000 + 5
    restore = truncate(ex, 50)
    assert len(cache.get(SYM, "15m", 300)) == 49
    wall[0] += STALE_RETRY_SEC
    assert len(cache.get(SYM, "15m", 300)) == 49  # второй обрезок подряд
    restore()
    wall[0] += 2 * STALE_RETRY_SEC - 1
    assert len(cache.get(SYM, "15m", 300)) == 49  # повтор с удвоением, не на каждом вызове
    wall[0] += 1
    df = cache.get(SYM, "15m", 300)
    assert len(df) == 300
    pd.testing.assert_frame_equal(df, client.fetch_closed_ohlcv(SYM, "15m", 300))
    assert cache.stats()["by_reason"]["verify"] == 2


def test_cold_truncated_answer_is_not_full_history():
    """Ревью P2-1, случай 1: холодный 4h-запрос вернул 50 строк — сравнить длину не с
    чем, и кеш отдавал 49 баров весь оставшийся бар (~3 ч) против 70 у прямого запроса.
    Короткий холодный ответ перепроверяется через 15 с; «вся история» — только когда два
    ответа подряд начинаются с одного бара."""
    wall, ex, client, cache = make_world()
    restore = truncate(ex, 50)
    assert len(cache.get(SYM, "4h", 260)) == 49
    restore()
    wall[0] += STALE_RETRY_SEC - 1
    assert len(cache.get(SYM, "4h", 260)) == 49
    wall[0] += 1
    assert len(cache.get(SYM, "4h", 260)) == 70  # перепроверка: история длиннее
    wall[0] += 2 * STALE_RETRY_SEC  # начало другое — еще одна сверка
    assert len(cache.get(SYM, "4h", 260)) == 70
    calls = len(ex.calls)
    for _ in range(30):  # подтверждено: до закрытия бара из кеша и совпадает с биржей
        wall[0] += 300
        pd.testing.assert_frame_equal(cache.get(SYM, "4h", 260), client.fetch_closed_ohlcv(SYM, "4h", 260))
    assert len(ex.calls) == calls + 30  # только прямые запросы сверки
    assert cache.stats()["by_reason"] == {
        "cold": 1, "limit": 0, "bar_close": 0, "stale": 0, "verify": 2, "confirm": 0, "ttl": 0,
    }


def test_stale_retry_never_skips_next_bar_close():
    """Ревью P2-2: нет бара перед формирующимся (неликвид, техработы) — запись в повторах с
    удвоением. Прямой запрос видит новый бар сразу после закрытия, кеш видел через 225 с.
    Повтор не позже закрытия следующего бара + люфт."""
    wall, ex, client, cache = make_world()
    d15 = ex._data["15m"]
    ex._data["15m"] = d15[d15["ts"] != T0_MS - TF_MS["15m"]].reset_index(drop=True)  # бара нет
    assert int(cache.get(SYM, "15m", 300)["ts"].iloc[-1]) == T0_MS - 2 * TF_MS["15m"]
    close = (T0_MS + TF_MS["15m"]) / 1000  # закрытие бара T0
    first_seen = None
    while wall[0] < close + 60:
        wall[0] += 1
        if int(cache.get(SYM, "15m", 300)["ts"].iloc[-1]) == T0_MS:
            first_seen = wall[0]
            break
    assert first_seen == close + GRACE_SEC  # прямой запрос — с close; кеш — после люфта
    pd.testing.assert_frame_equal(cache.get(SYM, "15m", 300), client.fetch_closed_ohlcv(SYM, "15m", 300))
    by = cache.stats()["by_reason"]
    assert by["stale"] <= 7 and by["bar_close"] == 0  # 15, 30, ... 300 с и повтор у закрытия


# --------------------------------------------------------- ошибки и инвалидация


def test_fetch_error_propagates_and_is_not_cached():
    wall, ex, _, cache = make_world()
    cache.get(SYM, "15m", 300)
    wall[0] = (T0_MS + TF_MS["15m"]) / 1000 + 5
    real = ex.fetch_ohlcv

    def boom(*a, **k):
        raise ConnectionError("reset")

    ex.fetch_ohlcv = boom
    with pytest.raises(ConnectionError):
        cache.get(SYM, "15m", 300)
    ex.fetch_ohlcv = real
    df = cache.get(SYM, "15m", 300)  # повтор сразу, а не через бар
    assert int(df["ts"].iloc[-1]) == T0_MS
    assert cache.stats()["errors"] == 1


def test_blacklisted_symbol_not_served_from_cache():
    """Символ умер (109415 на 1h): его 15m из кеша больше не отдается — как и прямой
    запрос, get отказывает «blacklisted», до биржи запрос не доходит."""
    wall, ex, client, cache = make_world()
    cache.get(SYM, "15m", 300)
    real = ex.fetch_ohlcv

    def paused(*a, **k):
        ex.calls.append(a[:2])
        raise RuntimeError('bingx {"code":109415,"msg":"SOL-USDT is pause currently"}')

    ex.fetch_ohlcv = paused
    with pytest.raises(RuntimeError, match="109415"):
        cache.get(SYM, "1h", 260)
    ex.fetch_ohlcv = real
    n = len(ex.calls)
    with pytest.raises(RuntimeError, match="blacklisted"):
        cache.get(SYM, "15m", 300)
    assert len(ex.calls) == n and client.is_blacklisted("SOL-USDT")
    # ревью P2-5: сбой биржи (109415) — errors, отказ без сети (черный список) — refused
    s = cache.stats()
    assert (s["keys"], s["errors"], s["refused"]) == (0, 1, 1)


def test_cooldown_refusal_counted_as_refused_not_error():
    """Ревью P2-5: отказы без обращения к бирже (cooldown, черный список) шли в errors —
    «errors около нуля» вводило в заблуждение. Теперь — refused; errors — только биржа."""
    wall, ex, client, cache = make_world()
    cache.get(SYM, "15m", 300)
    wall[0] = (T0_MS + TF_MS["15m"]) / 1000 + 5  # запись просрочена — пора к бирже
    client._enter_cooldown(wall[0] + 600, "exchange ban 109429")
    n = len(ex.calls)
    with pytest.raises(ExchangeCooldown):
        cache.get(SYM, "15m", 300)
    assert len(ex.calls) == n  # до биржи не дошло
    s = cache.stats()
    assert (s["errors"], s["refused"], s["keys"]) == (0, 1, 1)  # запись цела


def test_invalidate_by_symbol_timeframe_and_all():
    wall, ex, _, cache = make_world()
    for tf in ("15m", "1h"):
        cache.get(SYM, tf, 80)
        cache.get("ETH/USDT:USDT", tf, 80)
    assert len(ex.calls) == 4
    assert cache.invalidate("SOL-USDT", "15m") == 1
    cache.get(SYM, "15m", 80)
    cache.get(SYM, "1h", 80)
    assert len(ex.calls) == 5  # сброшен только (SOL, 15m)
    assert cache.invalidate(SYM) == 2
    assert cache.invalidate(timeframe="1h") == 1  # ETH 1h
    assert cache.invalidate() == 1  # ETH 15m
    assert cache.stats()["keys"] == 0 and cache.stats()["invalidated"] == 5


def test_returned_frames_are_copies():
    wall, ex, _, cache = make_world()
    a = cache.get(SYM, "15m", 100)
    a.loc[a.index[-1], "close"] = -1.0
    assert cache.get(SYM, "15m", 100)["close"].iloc[-1] != -1.0


# ------------------------------------------- правки закрытых баров и часы биржи


def test_revised_closed_bars_are_counted_logged_and_rechecked(caplog):
    """Ревью P2-3а: биржа дописывает объем закрытого бара 10 с (> люфта 3 с) — кеш
    замораживал первый снимок на весь следующий бар (объем 706 против 1008.5 у прямого
    запроса), и ни один счетчик этого не показывал. Теперь сверка пересечения окон на
    перезапросе: revised в счетчике, одно предупреждение в лог, и у ключа каждый новый бар
    перечитывается через STALE_RETRY_SEC — первый снимок живет не дольше 15 с."""
    wall, ex, client, cache = make_world(finalize_sec=10.0)
    with caplog.at_level(logging.WARNING, logger="oskar.data"):
        cache.get(SYM, "15m", 300)  # T0+7: бар T0-15m закрыт 7 с назад — объем недописан
        close = (T0_MS + TF_MS["15m"]) / 1000
        wall[0] = close + GRACE_SEC  # перезапрос на закрытии: бар T0-15m поправлен
        df = cache.get(SYM, "15m", 300)
        assert int(df["ts"].iloc[-1]) == T0_MS and cache.stats()["revised"] == 1
        wall[0] += STALE_RETRY_SEC - 1  # новый бар T0 тоже недописан — пока из кеша
        stale_vol = cache.get(SYM, "15m", 300)["volume"].iloc[-1]
        assert stale_vol != client.fetch_closed_ohlcv(SYM, "15m", 300)["volume"].iloc[-1]
        for dt in (0, 300, 880):  # перечитали: совпадает с прямым запросом до закрытия
            wall[0] = close + GRACE_SEC + STALE_RETRY_SEC + dt
            pd.testing.assert_frame_equal(cache.get(SYM, "15m", 300),
                                          client.fetch_closed_ohlcv(SYM, "15m", 300))
        # следующий бар: правок в пересечении нет, но ключ уже перечитывает каждый новый бар
        wall[0] = close + TF_MS["15m"] / 1000 + GRACE_SEC
        cache.get(SYM, "15m", 300)
        wall[0] += STALE_RETRY_SEC
        pd.testing.assert_frame_equal(cache.get(SYM, "15m", 300),
                                      client.fetch_closed_ohlcv(SYM, "15m", 300))
    s = cache.stats()
    assert s["by_reason"]["bar_close"] == 2 and s["by_reason"]["confirm"] == 2
    assert s["revised"] == 3  # T0-15m на закрытии, T0 и T0+15m на перечитывании
    warnings = [r.getMessage() for r in caplog.records if "revised" in r.getMessage()]
    assert len(warnings) == 1 and "SOL/USDT:USDT 15m" in warnings[0]  # дальше — только счетчик


def test_no_revisions_no_extra_requests():
    """Биржа закрытые бары не правит — сверка ничего не находит, лишних запросов нет."""
    wall, ex, _, cache = make_world()
    cache.get(SYM, "15m", 300)
    for bar in range(1, 5):
        wall[0] = (T0_MS + bar * TF_MS["15m"]) / 1000 + GRACE_SEC
        cache.get(SYM, "15m", 300)
        wall[0] += STALE_RETRY_SEC
        cache.get(SYM, "15m", 300)
    s = cache.stats()
    assert s["revised"] == 0 and s["by_reason"]["confirm"] == 0 and len(ex.calls) == 5


def test_clock_offset_from_exchange_moves_bar_boundaries(caplog):
    """Ревью P2-3б: наши часы отстают от биржи на 20 с — каждое обновление опаздывало на
    20 с, и это не было видно ни в одном счетчике. Смещение меряется по server time
    (при старте и раз в CLOCK_SYNC_SEC), границы баров — по часам биржи; > люфта —
    предупреждение в лог, значение — в /status."""
    skew = 20.0
    wall, ex, client, cache = make_world(skew_sec=skew, server_time=True)
    blind_wall, _, _, blind = make_world(skew_sec=skew)  # контроль: биржа без server time
    with caplog.at_level(logging.WARNING, logger="oskar.data"):
        cache.get(SYM, "15m", 300)
        blind.get(SYM, "15m", 300)
    assert ex.time_calls == 1
    assert cache.stats()["clock_offset_sec"] == skew and blind.stats()["clock_offset_sec"] is None
    assert any("clock offset to exchange +20.0s" in r.getMessage() for r in caplog.records)

    close_local = (T0_MS + TF_MS["15m"]) / 1000 - skew  # закрытие бара T0 по нашим часам
    wall[0] = blind_wall[0] = close_local + GRACE_SEC
    assert int(cache.get(SYM, "15m", 300)["ts"].iloc[-1]) == T0_MS  # вовремя по бирже
    assert int(blind.get(SYM, "15m", 300)["ts"].iloc[-1]) == T0_MS - TF_MS["15m"]  # опоздал бы
    pd.testing.assert_frame_equal(cache.get(SYM, "15m", 300), client.fetch_closed_ohlcv(SYM, "15m", 300))
    assert cache.stats()["by_reason"]["stale"] == 0

    wall[0] += CLOCK_SYNC_SEC  # повторный замер раз в 30 мин
    cache.get(SYM, "15m", 300)
    assert ex.time_calls == 2


def test_clock_sync_failure_retried_sooner_and_keeps_last_offset():
    wall, ex, _, cache = make_world(skew_sec=5.0, server_time=True)
    cache.get(SYM, "15m", 300)
    assert cache.stats()["clock_offset_sec"] == 5.0

    def down():
        ex.time_calls += 1
        raise ConnectionError("reset")

    ex.fetch_time = down
    wall[0] += CLOCK_SYNC_SEC
    cache.get(SYM, "15m", 300)
    assert ex.time_calls == 2 and cache.stats()["clock_offset_sec"] == 5.0  # прошлое смещение
    wall[0] += CLOCK_SYNC_RETRY_SEC - 1
    cache.get(SYM, "15m", 300)
    assert ex.time_calls == 2
    wall[0] += 1
    cache.get(SYM, "15m", 300)
    assert ex.time_calls == 3  # неудачный замер — повтор через 5 мин, не через 30
    assert cache.stats()["errors"] == 0  # замер часов — не сбой свечей


# ------------------------------------------- битое окно у потребителя (ревью P2-4)


def cycle_for(cache, tmp_path, context=("1h",)):
    cfg = CycleConfig(signal_timeframe="15m", context_timeframes=context, candles_limit=150,
                      min_score=30, cooldown_bars=1, smc=SmcConfig(swing_length=5))
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    return ShadowCycle(cache, state, CAPS, PROFILE, cfg), state


def test_data_error_in_cycle_drops_cache_entry(tmp_path, monkeypatch):
    """Ревью P2-4: у invalidate не было вызова в продакшене — битое окно жило до закрытия
    бара или рестарта. Анализ упал на окне из кеша — запись (символ, ТФ) долой, следующий
    тик идет к бирже; исключение — в отчет цикла, как было."""
    wall, ex, _, cache = make_world()
    cycle, _ = cycle_for(cache, tmp_path)

    def broken(df, cfg):
        raise ValueError("bad window: duplicated ts")

    monkeypatch.setattr("oskar.core.cycle.analyze", broken)
    report = cycle.run_once([SYM], 1000.0)
    assert report.errors and "bad window" in report.errors[0]
    assert cache.stats()["invalidated"] == 1 and cache.stats()["keys"] == 0
    monkeypatch.undo()
    n = len(ex.calls)
    cycle.run_once([SYM], 1000.0)
    assert ex.calls[n][1] == "15m" and cache.stats()["by_reason"]["cold"] == 3  # снова к бирже


def test_context_timeframe_error_drops_only_that_timeframe(tmp_path, monkeypatch):
    wall, ex, _, cache = make_world()
    cycle, _ = cycle_for(cache, tmp_path)
    import oskar.core.cycle as cycle_mod

    real = cycle_mod.compute

    def compute(df):
        if len(df) == 100:  # окно 1h (вся история — 100 бар)
            raise KeyError("close")
        return real(df)

    monkeypatch.setattr("oskar.core.cycle.compute", compute)
    cycle.run_once([SYM], 1000.0)
    assert cache.stats()["invalidated"] == 1
    assert [k[1] for k in cache._store] == ["15m"]  # 15m цел, 1h сброшен


def test_short_history_error_keeps_cache_entry(tmp_path):
    """Истории меньше 60 баров (молодой листинг) — свойство символа, не битые данные:
    InsufficientHistory записи не сбрасывает, иначе символ ходил бы к бирже каждый тик."""
    wall, ex, _, cache = make_world(n1h=50)
    cycle, _ = cycle_for(cache, tmp_path)
    report = cycle.run_once([SYM], 1000.0)
    assert report.errors and "need >=60" in report.errors[0]
    assert issubclass(InsufficientHistory, ValueError)
    assert cache.stats()["invalidated"] == 0 and cache.stats()["keys"] == 2


def test_data_error_in_resolver_drops_cache_entry(tmp_path, monkeypatch):
    wall, ex, _, cache = make_world()
    cycle, state = cycle_for(cache, tmp_path)
    created = datetime.fromtimestamp(T0_MS / 1000 - 3600, UTC).isoformat()
    state.journal.execute(
        "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, sl_price, "
        "tp_price) VALUES ('d1', 'shadow', ?, 100, 1, 2)",
        (json.dumps({"symbol": SYM, "direction": "long", "ts": created}),),
    )
    state.journal.commit()

    def broken(df, r):
        raise IndexError("single positional indexer is out-of-bounds")

    monkeypatch.setattr("oskar.core.cycle._cf_outcome", broken)
    with pytest.raises(IndexError):
        cycle.resolve_counterfactuals(SYM)
    assert cache.stats()["invalidated"] == 1
    monkeypatch.undo()
    assert cycle.resolve_counterfactuals(SYM) == 1  # следующий проход — свежее окно с биржи
    assert cache.stats()["by_reason"]["cold"] == 2


def test_data_error_in_scanner_drops_cache_entry(monkeypatch):
    wall, ex, _, cache = make_world()
    cache.get(SYM, "1h", 80)

    def broken(df):
        raise ValueError("NaN in close")

    monkeypatch.setattr("oskar.data.scanner.compute", broken)
    assert quick_score_one(cache, SYM, 0) is None
    assert cache.stats()["invalidated"] == 1

    def short(df):
        raise InsufficientHistory("need >=60 closed candles, got 40")

    cache.get(SYM, "1h", 80)
    monkeypatch.setattr("oskar.data.scanner.compute", short)
    assert quick_score_one(cache, SYM, 0) is None
    assert cache.stats()["invalidated"] == 1  # мало истории — запись остается


# ----------------------------------------------------------------- метрика


def test_stats_and_log_every_30_min(caplog):
    wall, ex, _, cache = make_world()
    with caplog.at_level(logging.INFO, logger="oskar.data"):
        cache.get(SYM, "15m", 300)
        for _ in range(9):
            cache.get(SYM, "15m", 300)
        cache.get(SYM, "1h", 260)
        assert not [r for r in caplog.records if "candle cache" in r.getMessage()]
        wall[0] += STATS_LOG_SEC
        cache.get(SYM, "15m", 300)  # первый вызов после 30 мин пишет сводку (и идет за баром)
    lines = [r.getMessage() for r in caplog.records if "candle cache" in r.getMessage()]
    assert len(lines) == 1
    assert "2 exchange requests" in lines[0] and "9 hits" in lines[0] and "cold 2" in lines[0]
    assert "0 errors, 0 refused, 0 revised" in lines[0] and "clock offset ?" in lines[0]
    s = cache.stats()
    assert s["requests"] == 3 and s["hits"] == 9 and s["by_reason"]["bar_close"] == 1


def test_runner_status_shows_cache_counters(tmp_path):
    from oskar.core.shadow_runner import ShadowRunner

    wall, ex, client, cache = make_world()
    cache.get(SYM, "15m", 300)
    cache.get(SYM, "15m", 300)
    runner = ShadowRunner(
        object(), cache, client, RuntimeState(journal=connect(tmp_path / "j.db")),
        top_n=5, min_quote_volume=0, tick_seconds=30,
    )
    health = runner.data_health()
    assert health["candle_cache"]["requests"] == 1 and health["candle_cache"]["hits"] == 1
    assert {"refused", "revised", "clock_offset_sec"} <= set(health["candle_cache"])
    assert "cooldown_sec" in health  # срез клиента на месте


# ------------------------------------- эквивалентность: цикл с кешем и без кеша


class DirectFeed:
    """Эталон «без кеша»: каждый get — запрос к бирже."""

    def __init__(self, client):
        self._client = client

    def get(self, symbol, timeframe, limit=300):
        return self._client.fetch_closed_ohlcv(symbol, timeframe, limit)


class Recorder:
    def __init__(self, inner, wall):
        self._inner = inner
        self._wall = wall
        self.seen: list[tuple] = []

    def get(self, symbol, timeframe, limit=300):
        df = self._inner.get(symbol, timeframe, limit)
        self.seen.append((symbol, timeframe, limit, self._wall[0], df.copy()))
        return df


def _journal_rows(state):
    def norm(js):
        return None if js is None else json.dumps(json.loads(js), sort_keys=True)

    dec = [
        (r["decision_id"], r["outcome"], r["outcome_detail"], norm(r["candidate_json"]),
         norm(r["risk_json"]))
        for r in state.journal.execute("SELECT * FROM decisions ORDER BY decision_id").fetchall()
    ]
    cf = [
        (r["decision_id"], r["cohort"], r["entry_price"], r["sl_price"], r["tp_price"],
         r["hypothetical_outcome"])
        for r in state.journal.execute("SELECT * FROM counterfactuals ORDER BY decision_id").fetchall()
    ]
    return dec, cf


def test_cycle_with_cache_sees_same_data_and_decides_the_same(tmp_path, monkeypatch):
    """Доказательство «поведение не меняется» (ст. 5 — инвариант не нужен): два мира на
    одних часах и одной истории — цикл+резолвер+скан через кеш и напрямую в биржу.
    Каждый get отдает бит-в-бит один DataFrame, журналы decisions/counterfactuals совпадают,
    кеш при этом ходит к бирже только на закрытиях баров."""
    wall_a, ex_a, _, cache = make_world()
    wall_b, ex_b, client_b, _ = make_world()
    wall = [wall_a[0]]

    class FakeDT(datetime):  # ts кандидатов/вердиктов — на фейковых часах (резолвер их сравнивает)
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(wall[0], tz or UTC)

    monkeypatch.setattr("oskar.detect.options.datetime", FakeDT)
    monkeypatch.setattr("oskar.risk.ladder.datetime", FakeDT)

    # окно 150 (не 300) — только ради скорости analyze; история 400 бар, окно полное
    cfg = CycleConfig(signal_timeframe="15m", context_timeframes=("1h",), candles_limit=150,
                      min_score=30, cooldown_bars=1, smc=SmcConfig(swing_length=5))
    feeds = {"cache": Recorder(cache, wall), "direct": Recorder(DirectFeed(client_b), wall)}
    states = {k: RuntimeState(journal=connect(tmp_path / f"{k}.db")) for k in feeds}
    cycles = {k: ShadowCycle(feeds[k], states[k], CAPS, PROFILE, cfg) for k in feeds}

    # 12 баров 15m (3 закрытия 1h): тики после закрытия (+7 с), внутри бара и за 7 с до закрытия
    ticks = [T0_MS / 1000 + bar * 900 + off for bar in range(12) for off in (7, 452, 893)]
    reports = {k: [] for k in feeds}
    for i, t in enumerate(ticks):
        wall[0] = wall_a[0] = wall_b[0] = t
        for k in feeds:
            reports[k].append(vars(cycles[k].run_once([SYM], 1000.0)))
            for sym in cycles[k].unresolved_symbols():
                cycles[k].resolve_counterfactuals(sym)
            if i % 6 == 0:  # пересборка вотчлиста: 1h x 80 — тот же ключ, что 1h x 260 цикла
                scan(feeds[k], [SYM], top_n=1, min_quote_volume=0)

    seen_a, seen_b = feeds["cache"].seen, feeds["direct"].seen
    assert len(seen_a) == len(seen_b) > len(ticks)
    for (sa, tfa, la, ta, dfa), (sb, tfb, lb, tb, dfb) in zip(seen_a, seen_b, strict=True):
        assert (sa, tfa, la, ta) == (sb, tfb, lb, tb)
        pd.testing.assert_frame_equal(dfa, dfb)  # бит-в-бит, включая dtypes
        assert int(dfa["ts"].iloc[-1]) + TF_MS[tfa] <= ta * 1000  # только закрытые бары

    assert reports["cache"] == reports["direct"]
    rows_a, rows_b = _journal_rows(states["cache"]), _journal_rows(states["direct"])
    assert rows_a == rows_b
    dec, cf = rows_a
    assert len(dec) >= 3  # решения есть — сравнение не пустое
    assert any(r[-1] is not None for r in cf)  # и резолвер успел дорешать исходы

    # нагрузка: без кеша — запрос на каждый get; с кешем — один на ключ за бар
    assert len(ex_b.calls) == len(seen_b)
    # 15m: старт + 11 закрытий; 1h: старт + перепроверка короткой истории (ревью P2-1) +
    # 2 закрытия (скан 1h x 80 — из окна цикла 1h x 260)
    assert Counter(c[1] for c in ex_a.calls) == {"15m": 12, "1h": 4}
    s = cache.stats()
    assert s["by_reason"]["verify"] == 1 and s["by_reason"]["confirm"] == 0
    assert (s["revised"], s["errors"], s["refused"], s["invalidated"]) == (0, 0, 0, 0)


def test_cycle_load_two_to_three_requests_per_minute():
    """Оценка из отчета (3г): цикл intraday — 20 символов, тик 30 с, ТФ 15m x 300 +
    1h/4h/1d x 260 — было ~65 запросов в минуту на TTL. С кешем по закрытию бара —
    в среднем ~1.8/мин (15m 4 раза в час, 1h раз в час, 4h/1d — реже)."""
    wall, ex, _, cache = make_world()
    symbols = [f"S{i}/USDT:USDT" for i in range(4)]  # 4 символа, пересчет на 20 — линейный
    plan = [("15m", 300), ("1h", 260), ("4h", 260), ("1d", 260)]
    for sym in symbols:  # прогрев: первый проход — по запросу на ключ
        for tf, limit in plan:
            cache.get(sym, tf, limit)
    warm = len(ex.calls)
    hours = 4
    for _ in range(hours * 120):  # тик 30 с
        wall[0] += 30
        for sym in symbols:
            for tf, limit in plan:
                cache.get(sym, tf, limit)
    by_tf = Counter(c[1] for c in ex.calls[warm:])
    n = len(symbols)
    assert warm == 4 * n
    # 1d за 4 часа не закрылся; 1h/4h/1d короче 260 — по одной перепроверке истории на ключ
    assert by_tf == {"15m": 4 * hours * n, "1h": hours * n + n, "4h": n + n, "1d": n}
    per_min_20 = (len(ex.calls) - warm) / (hours * 60) * 20 / n
    assert per_min_20 <= 3.0  # было ~65
