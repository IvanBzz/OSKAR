"""Клиент рыночных данных — обертка ccxt BingX.

Дисциплина ЗАКРЫТЫХ свечей: ccxt возвращает последнюю, еще ФОРМИРУЮЩУЮСЯ
свечу последней строкой — она всегда отбрасывается. Детект-слой видит только
закрытые бары (look-ahead защита начинается уже здесь).

Биржевой объект инжектится (тесты — фейк, прод — фабрика make_bingx_exchange).
Символы: на вход канонические ("SOL/USDT:USDT") — ccxt unified принимает их
напрямую, конвертация не нужна.
"""

import time
from typing import Any, Protocol

import pandas as pd

from oskar.data.symbols import canonical

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
RETRIES = 2
RETRY_DELAY_SEC = 1.0
UNIVERSE_TTL_SEC = 4 * 3600  # v1: кеш списка пар 4 часа


class ExchangeLike(Protocol):
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list]: ...
    def load_markets(self, reload: bool = False) -> dict[str, Any]: ...


class MarketDataClient:
    def __init__(
        self,
        exchange: ExchangeLike,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self._ex = exchange
        self._clock = clock
        self._sleep = sleep
        self._universe: list[str] | None = None
        self._universe_at: float = -UNIVERSE_TTL_SEC

    def _with_retry(self, fn, *args, **kwargs):
        last: Exception | None = None
        for attempt in range(1 + RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - ccxt кидает разные Network/Exchange ошибки
                last = e
                if attempt < RETRIES:
                    self._sleep(RETRY_DELAY_SEC * (attempt + 1))
        raise last  # type: ignore[misc]

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        """OHLCV ТОЛЬКО закрытых свечей: последняя (формирующаяся) отброшена.

        Запрашивается limit+1, чтобы после отброса осталось ровно limit.
        Возвращает DataFrame [open, high, low, close, volume] c RangeIndex
        и колонкой ts (миллисекунды открытия бара).
        """
        sym = canonical(symbol)
        raw = self._with_retry(self._ex.fetch_ohlcv, sym, timeframe, limit + 1)
        if not raw or len(raw) < 2:
            raise RuntimeError(f"not enough candles for {sym} {timeframe}: got {len(raw or [])}")
        df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
        df = df.iloc[:-1].reset_index(drop=True)  # ФОРМИРУЮЩАЯСЯ свеча отброшена
        return df.astype(
            {"open": float, "high": float, "low": float, "close": float, "volume": float}
        )

    def universe(self) -> list[str]:
        """Активные USDT-перпы, канонические символы. Кеш 4 часа."""
        now = self._clock()
        if self._universe is not None and now - self._universe_at < UNIVERSE_TTL_SEC:
            return self._universe
        markets = self._with_retry(self._ex.load_markets, True)
        out = []
        for m in markets.values():
            if (
                m.get("swap")
                and m.get("active")
                and m.get("quote") == "USDT"
                and m.get("settle") == "USDT"
            ):
                out.append(canonical(m["symbol"]))
        self._universe = sorted(set(out))
        self._universe_at = now
        return self._universe


def make_bingx_exchange(api_key: str = "", secret: str = "", sandbox: bool = True):
    """Фабрика ccxt BingX. Для публичных данных ключи не обязательны."""
    import ccxt

    ex = ccxt.bingx({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        # defaultType=swap ОБЯЗАТЕЛЕН: без него fetch_balance уходит в SPOT
        # реального аккаунта даже в sandbox (VST — это swap-хост)
        "options": {"adjustForTimeDifference": True, "defaultType": "swap"},
    })
    if sandbox:
        ex.set_sandbox_mode(True)
    return ex
