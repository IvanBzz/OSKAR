"""Кеш OHLCV с TTL per-timeframe (v1: 30с-1ч в зависимости от ТФ).

Инжектируемые часы — тесты управляют временем. Ключ кеша: (symbol, timeframe).
Кеш отдает ссылку на неизменяемый снепшот (копию) — мутации потребителя не
портят кеш.
"""

import time
from dataclasses import dataclass

import pandas as pd

from oskar.data.client import MarketDataClient
from oskar.data.symbols import canonical

TTL_SEC: dict[str, float] = {
    "1m": 10,
    "5m": 20,
    "15m": 30,
    "1h": 60,
    "4h": 300,
    "1d": 900,
}
DEFAULT_TTL_SEC = 60.0


@dataclass
class _Entry:
    df: pd.DataFrame
    at: float


class CandleCache:
    def __init__(self, client: MarketDataClient, *, clock=time.monotonic):
        self._client = client
        self._clock = clock
        self._store: dict[tuple[str, str], _Entry] = {}

    def get(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        key = (canonical(symbol), timeframe)
        now = self._clock()
        ttl = TTL_SEC.get(timeframe, DEFAULT_TTL_SEC)
        entry = self._store.get(key)
        if entry is not None and now - entry.at < ttl and len(entry.df) >= limit:
            return entry.df.iloc[-limit:].reset_index(drop=True).copy()
        df = self._client.fetch_closed_ohlcv(key[0], timeframe, limit)
        self._store[key] = _Entry(df=df, at=now)
        return df.copy()

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._store.clear()
        else:
            sym = canonical(symbol)
            for key in [k for k in self._store if k[0] == sym]:
                del self._store[key]
