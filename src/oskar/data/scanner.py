"""Двухступенчатый сканер (v1-паттерн: ~600 пар -> quick filter 1h -> top-20).

Ступень 1 (quick): дешевый скоринг по 1h-индикаторам (объем, RSI-экстремум,
определенность тренда, подвижность ATR) — отбирает кандидатов для дорогой
ступени 2 (полный SMC-анализ + score.py в цикле демона).

Ошибка по одному символу (мало истории, делистинг) — символ пропускается,
скан продолжается (graceful degradation, принцип v1).
"""

from dataclasses import dataclass

from oskar.detect.indicators import compute, tf_trend

QUICK_TIMEFRAME = "1h"
QUICK_CANDLES = 80
# Пол ликвидности: средний ЧАСОВОЙ оборот в quote (volume*close по 1h-барам).
# Живая проба 2026-08-26: без пола топ-20 забивают мемкоины с нулевым оборотом
# (RSI-экстремум неликвида = дутые очки, спреды убийственные).
MIN_HOURLY_QUOTE_VOLUME_USD = 200_000.0


@dataclass(frozen=True)
class QuickScore:
    symbol: str
    score: int
    volume_ratio: float
    rsi: float
    trend: str
    atr_pct: float


def quick_score_one(
    cache, symbol: str, min_quote_volume: float = MIN_HOURLY_QUOTE_VOLUME_USD
) -> QuickScore | None:
    """None = символ не скорится (неликвид/мало данных/ошибка) — пропуск."""
    try:
        df = cache.get(symbol, QUICK_TIMEFRAME, QUICK_CANDLES)
        avg_quote_vol = float((df["volume"] * df["close"]).tail(20).mean())
        if avg_quote_vol < min_quote_volume:
            return None
        ind = compute(df)
    except Exception:  # noqa: BLE001 - любой сбой одного символа не валит скан
        return None

    score = 0
    if ind.volume_ratio >= 2.0:
        score += 40
    elif ind.volume_ratio >= 1.5:
        score += 30

    if ind.rsi <= 30 or ind.rsi >= 70:
        score += 20
    elif ind.rsi <= 35 or ind.rsi >= 65:
        score += 10

    trend = tf_trend(ind)
    if trend != "mixed":
        score += 20

    if ind.atr_pct >= 0.5:
        score += 10

    return QuickScore(
        symbol=symbol, score=score, volume_ratio=round(ind.volume_ratio, 2),
        rsi=round(ind.rsi, 1), trend=trend, atr_pct=round(ind.atr_pct, 3),
    )


def quick_filter(
    cache,
    symbols: list[str],
    top_n: int = 20,
    min_quote_volume: float = MIN_HOURLY_QUOTE_VOLUME_USD,
) -> list[QuickScore]:
    """Скорит все символы, возвращает top_n по убыванию (при равенстве — по имени)."""
    scored = [
        qs for s in symbols
        if (qs := quick_score_one(cache, s, min_quote_volume)) is not None
    ]
    scored.sort(key=lambda q: (-q.score, q.symbol))
    return scored[:top_n]
