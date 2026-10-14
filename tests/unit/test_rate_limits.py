"""Ревью 2026-10-13, #25: rate limit останавливает весь kline-клиент, а не хоронит символ."""

from datetime import UTC, datetime
from email.utils import format_datetime

import ccxt
import pytest

from oskar.data.candles import CandleCache
from oskar.data.client import ExchangeCooldown, MarketDataClient, classify_error
from oskar.data.scanner import scan
from tests.unit.test_data_errors import WALL0, ExchangeError, ScriptedExchange, ban_text


@pytest.mark.parametrize("error", [
    ccxt.DDoSProtection("slow down"),
    ccxt.RateLimitExceeded("limit reached"),
    ccxt.NetworkError("bingx GET /klines 429 Too Many Requests"),
    ccxt.OperationFailed('bingx {"code":429}'),
    ExchangeError("HTTP 429"),
])
def test_rate_limit_stops_requests_for_all_symbols_and_recovers(error):
    clock = [100.0]
    sleeps = []
    ex = ScriptedExchange({"A/USDT:USDT": [error]})
    client = MarketDataClient(ex, clock=lambda: clock[0], wall=lambda: WALL0, sleep=sleeps.append)
    assert classify_error(error) == "cooldown"
    with pytest.raises(ExchangeCooldown) as caught:
        client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)
    assert caught.value.__cause__ is error
    assert client.cooldown_remaining() == 60
    assert client.errors_in_window() == 0 and client.dead_symbols == {}
    assert sleeps == [] and ex.calls == ["A/USDT:USDT"]
    for symbol in ("A/USDT:USDT", "B/USDT:USDT"):
        with pytest.raises(ExchangeCooldown):
            client.fetch_closed_ohlcv(symbol, "1h", 100)
    assert ex.calls == ["A/USDT:USDT"]
    clock[0] += 60
    assert len(client.fetch_closed_ohlcv("B/USDT:USDT", "1h", 100)) == 100
    assert len(client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)) == 100


@pytest.mark.parametrize(("hint", "wait"), [
    ("Retry-After: 120", 120),
    ("retry-after: 2", 2),
    ('{"retry-after": "90"}', 90),
    ("{'Retry-After': '30.5'}", 30.5),
    ("Retry-After=45; scope=ip", 45),
    ("Retry-After: 0", 0),
    ("Retry-After: invalid", 60),
    ("Retry-After: -5", 60),
    ("Retry-After: " + format_datetime(datetime.fromtimestamp(WALL0 + 180, UTC), usegmt=True), 180),
])
def test_rate_limit_honors_retry_after(hint, wait):
    ex = ScriptedExchange({"A/USDT:USDT": [ccxt.RateLimitExceeded(hint)]})
    client = MarketDataClient(ex, clock=lambda: 100.0, wall=lambda: WALL0)
    with pytest.raises(ExchangeCooldown):
        client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)
    assert client.cooldown_remaining() == pytest.approx(wait)
    assert client.errors_in_window() == 0 and not client.dead_symbols


def test_cooldown_starts_when_response_arrives_after_transient_retry():
    clock = [0.0]
    sleeps = []

    class DelayedExchange(ScriptedExchange):
        def fetch_ohlcv(self, *args, **kwargs):
            clock[0] += 10
            return super().fetch_ohlcv(*args, **kwargs)

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    ex = DelayedExchange({"A/USDT:USDT": [ConnectionError("reset"), ccxt.RateLimitExceeded("limit")]})
    client = MarketDataClient(ex, clock=lambda: clock[0], sleep=sleep)
    with pytest.raises(ExchangeCooldown):
        client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)
    assert clock[0] == 21 and client.cooldown_remaining() == 60
    assert len(ex.calls) == 2 and sleeps == [1.0]
    assert client.errors_in_window() == 0


def test_bingx_ban_takes_priority_over_ccxt_network_inheritance():
    ex = ScriptedExchange({"A/USDT:USDT": [ccxt.DDoSProtection(ban_text(int((WALL0 + 600) * 1000)))]})
    client = MarketDataClient(ex, clock=lambda: 0.0, wall=lambda: WALL0)
    with pytest.raises(ccxt.DDoSProtection):
        client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)
    assert client.cooldown_remaining() == 605
    assert len(ex.calls) == 1 and client.errors_in_window() == 0 and not client.dead_symbols


def test_rate_limit_aborts_559_symbol_scan_even_with_cached_tail():
    symbols = [f"S{i}/USDT:USDT" for i in range(559)]
    ex = ScriptedExchange({symbols[0]: [ccxt.RateLimitExceeded("Retry-After: 120")]})
    client = MarketDataClient(ex, clock=lambda: 0.0)
    cache = CandleCache(client, clock=lambda: 0.0)
    for symbol in symbols[1:]:
        cache.get(symbol, "1h", 80)
    ex.calls.clear()

    top, summary = scan(cache, symbols, min_quote_volume=0)
    assert top == [] and summary.total == 559 and summary.scored == 0
    assert summary.aborted is not None and "rate limit" in summary.aborted
    assert not summary.errors
    assert ex.calls == [symbols[0]]
    assert client.errors_in_window() == 0 and not client.dead_symbols


def test_price_like_429_is_not_a_rate_limit():
    """Цена «0.429» в тексте ошибки — не HTTP 429: глобальная пауза из-за нее была бы ложной."""
    price_err = ccxt.ExchangeError('bingx {"code":80014,"msg":"stop price 0.429 invalid"}')
    assert classify_error(price_err) == "symbol"
    assert classify_error(ccxt.ExchangeError('bingx {"code":109429,"msg":"over 10 error"}')) == "cooldown"
