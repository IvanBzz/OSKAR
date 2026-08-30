"""Клиент данных и кеш свечей: закрытые свечи, ретраи, TTL, universe."""

import pytest

from oskar.data.candles import CandleCache
from oskar.data.client import MarketDataClient


def make_raw(n: int, start_price: float = 100.0):
    """n сырых ccxt-свечей [ts, o, h, l, c, v]."""
    return [
        [1_700_000_000_000 + i * 900_000, start_price + i, start_price + i + 1,
         start_price + i - 1, start_price + i + 0.5, 1000 + i]
        for i in range(n)
    ]


class FakeExchange:
    def __init__(self, candles=None, markets=None, fail_times: int = 0):
        self._candles = candles if candles is not None else make_raw(301)
        self._markets = markets or {}
        self.fail_times = fail_times
        self.ohlcv_calls = 0
        self.markets_calls = 0

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        """Сигнатура ccxt: 3-й позиционный — since! Фейк с неправильной
        сигнатурой замаскировал боевой баг (limit улетал в since)."""
        self.ohlcv_calls += 1
        self.last_since, self.last_limit = since, limit
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("transient")
        assert limit is not None, "client must pass limit as keyword"
        return self._candles[-limit:]

    def load_markets(self, reload=False):
        self.markets_calls += 1
        return self._markets


def make_client(ex, t=None):
    clock = (lambda: t[0]) if t is not None else (lambda: 0.0)
    return MarketDataClient(ex, clock=clock, sleep=lambda s: None)


def test_forming_candle_dropped():
    """Последняя (формирующаяся) свеча ВСЕГДА отброшена; осталось ровно limit."""
    ex = FakeExchange(make_raw(301))
    df = make_client(ex).fetch_closed_ohlcv("SOL/USDT:USDT", "15m", limit=300)
    assert len(df) == 300
    # последняя строка df == предпоследняя сырая свеча
    assert df["close"].iloc[-1] == make_raw(301)[-2][4]
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_retry_recovers_from_transient_errors():
    ex = FakeExchange(fail_times=2)  # 2 сбоя, 3-я попытка успешна
    df = make_client(ex).fetch_closed_ohlcv("SOLUSDT", "15m", 100)
    assert len(df) == 100
    assert ex.ohlcv_calls == 3


def test_retry_exhausted_raises():
    ex = FakeExchange(fail_times=10)
    with pytest.raises(ConnectionError):
        make_client(ex).fetch_closed_ohlcv("SOL/USDT:USDT", "15m", 100)


def test_limit_passed_as_keyword_never_positional():
    """Регресс живого бага: limit третьим позиционным = since 1970 года ->
    пустые ответы + 109415 + бан API."""
    ex = FakeExchange(make_raw(301))
    make_client(ex).fetch_closed_ohlcv("SOL/USDT:USDT", "15m", 300)
    assert ex.last_limit == 301
    assert ex.last_since is None


class ExchangeError(Exception):
    """Имитация ccxt.ExchangeError (не сетевая)."""


def test_exchange_errors_not_retried_and_blacklisted():
    """Биржевые ошибки не долбим повторами И заносим символ в черный список:
    повторы к мертвым листингам копят счетчик BingX -> бан (>10 x 109415
    за 15 мин, поймано вживую дважды)."""

    class FailingExchange(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
            self.ohlcv_calls += 1
            raise ExchangeError("bingx 109415 invalid something")

    ex = FailingExchange()
    client = make_client(ex)
    with pytest.raises(ExchangeError):
        client.fetch_closed_ohlcv("SOL/USDT:USDT", "15m", 100)
    assert ex.ohlcv_calls == 1  # ровно одна попытка, без ретраев
    with pytest.raises(RuntimeError, match="blacklisted"):
        client.fetch_closed_ohlcv("SOL/USDT:USDT", "15m", 100)
    assert ex.ohlcv_calls == 1  # повторный запрос до биржи НЕ дошел


def test_network_errors_not_blacklisted():
    """Сетевой сбой — не приговор символу: после ретраев он остается живым."""
    ex = FakeExchange(fail_times=10)
    client = make_client(ex)
    with pytest.raises(ConnectionError):
        client.fetch_closed_ohlcv("SOL/USDT:USDT", "15m", 100)
    assert "SOL/USDT:USDT" not in client.dead_symbols


def test_universe_filters_and_caches():
    markets = {
        "SOL/USDT:USDT": {"symbol": "SOL/USDT:USDT", "swap": True, "active": True,
                          "quote": "USDT", "settle": "USDT"},
        "BTC/USDT": {"symbol": "BTC/USDT", "swap": False, "active": True,
                     "quote": "USDT", "settle": None},          # спот — мимо
        "ETH/USDC:USDC": {"symbol": "ETH/USDC:USDC", "swap": True, "active": True,
                          "quote": "USDC", "settle": "USDC"},   # не USDT — мимо
        "OLD/USDT:USDT": {"symbol": "OLD/USDT:USDT", "swap": True, "active": False,
                          "quote": "USDT", "settle": "USDT"},   # неактивен — мимо
    }
    t = [0.0]
    ex = FakeExchange(markets=markets)
    client = make_client(ex, t)
    assert client.universe() == ["SOL/USDT:USDT"]
    t[0] = 3600.0
    client.universe()  # в пределах 4ч — из кеша
    assert ex.markets_calls == 1
    t[0] = 5 * 3600.0
    client.universe()  # TTL истек — перезагрузка
    assert ex.markets_calls == 2


def test_candle_cache_ttl():
    t = [0.0]
    ex = FakeExchange(make_raw(301))
    cache = CandleCache(make_client(ex), clock=lambda: t[0])

    cache.get("SOL/USDT:USDT", "15m", 100)
    cache.get("SOL-USDT", "15m", 100)  # тот же символ в другом формате — кеш-хит
    assert ex.ohlcv_calls == 1

    t[0] = 29.0
    cache.get("SOL/USDT:USDT", "15m", 100)  # TTL 15m = 30с — еще хит
    assert ex.ohlcv_calls == 1

    t[0] = 31.0
    cache.get("SOL/USDT:USDT", "15m", 100)  # протух — перезапрос
    assert ex.ohlcv_calls == 2


def test_candle_cache_returns_copies():
    ex = FakeExchange(make_raw(301))
    cache = CandleCache(make_client(ex))
    a = cache.get("SOL/USDT:USDT", "15m", 100)
    a.loc[0, "close"] = -1.0  # мутация потребителя
    b = cache.get("SOL/USDT:USDT", "15m", 100)
    assert b["close"].iloc[0] != -1.0  # кеш не испорчен


def test_cache_smaller_limit_served_from_cache():
    ex = FakeExchange(make_raw(301))
    cache = CandleCache(make_client(ex))
    cache.get("SOL/USDT:USDT", "15m", 300)
    small = cache.get("SOL/USDT:USDT", "15m", 100)
    assert len(small) == 100 and ex.ohlcv_calls == 1
