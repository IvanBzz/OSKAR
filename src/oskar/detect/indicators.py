"""Классические индикаторы (pandas-ta) + мульти-ТФ тренд.

pandas-ta, не ta-lib (Windows без C-компилятора — урок v1).
Все расчеты — по закрытым свечам; последняя строка df = последняя ЗАКРЫТАЯ.
"""

from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta

# веса таймфреймов для мульти-ТФ тренда (v1: старшие ТФ якорят)
TF_WEIGHTS = {"15m": 1.0, "1h": 2.0, "4h": 3.5, "1d": 5.0}


@dataclass(frozen=True)
class IndicatorSet:
    rsi: float
    ema20: float
    ema50: float
    ema200: float | None  # None при короткой истории
    atr: float
    atr_pct: float  # ATR как % от цены
    macd_hist: float
    macd_cross: str  # bull_cross | bear_cross | none  (в последних 3 барах)
    volume_ratio: float  # последний объем / MA20 объема
    close: float


def compute(df: pd.DataFrame) -> IndicatorSet:
    """Индикаторы по одному ТФ. Минимум ~60 закрытых свечей."""
    if len(df) < 60:
        raise ValueError(f"need >=60 closed candles, got {len(df)}")
    close = df["close"]

    rsi = ta.rsi(close, length=14)
    ema20 = ta.ema(close, length=20)
    ema50 = ta.ema(close, length=50)
    ema200 = ta.ema(close, length=200) if len(df) >= 200 else None
    atr = ta.atr(df["high"], df["low"], close, length=14)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    vol_ma = df["volume"].rolling(20).mean()

    hist = macd.iloc[:, 1]  # колонка MACDh
    cross = "none"
    recent = hist.iloc[-3:]
    if len(recent.dropna()) == 3:
        if recent.iloc[-1] > 0 and (recent < 0).any():
            cross = "bull_cross"
        elif recent.iloc[-1] < 0 and (recent > 0).any():
            cross = "bear_cross"

    last_close = float(close.iloc[-1])
    last_atr = float(atr.iloc[-1])
    return IndicatorSet(
        rsi=float(rsi.iloc[-1]),
        ema20=float(ema20.iloc[-1]),
        ema50=float(ema50.iloc[-1]),
        ema200=float(ema200.iloc[-1]) if ema200 is not None else None,
        atr=last_atr,
        atr_pct=last_atr / last_close * 100.0,
        macd_hist=float(hist.iloc[-1]),
        macd_cross=cross,
        volume_ratio=float(df["volume"].iloc[-1] / vol_ma.iloc[-1]),
        close=last_close,
    )


def tf_trend(ind: IndicatorSet) -> str:
    """Тренд одного ТФ: bullish | bearish | mixed по EMA20/50 и цене."""
    if ind.close > ind.ema20 > ind.ema50:
        return "bullish"
    if ind.close < ind.ema20 < ind.ema50:
        return "bearish"
    return "mixed"


def multi_tf_trend(per_tf: dict[str, IndicatorSet]) -> str:
    """Взвешенный мульти-ТФ тренд: bullish | bearish | mixed.

    Согласие >= 60% суммарного веса требуется для направления (иначе mixed).
    """
    if not per_tf:
        return "mixed"
    bull = bear = total = 0.0
    for tf, ind in per_tf.items():
        w = TF_WEIGHTS.get(tf, 1.0)
        total += w
        t = tf_trend(ind)
        if t == "bullish":
            bull += w
        elif t == "bearish":
            bear += w
    if bull / total >= 0.6:
        return "bullish"
    if bear / total >= 0.6:
        return "bearish"
    return "mixed"
