"""Opportunity score 0-100 — детерминированный фильтр эскалации к Scout.

v1-скоринг перевзвешен в сторону SMC (решение плана): SMC-конфлюэнс 40,
мульти-ТФ тренд 25, объем 15, RSI 10, MACD 10. Порог эскалации —
settings.scanner.min_opportunity_score (70).

Направление выводится из СТРУКТУРЫ (последний BOS/CHoCH), не из индикаторов:
индикаторы добавляют очки, только если согласны с структурой.
"""

from dataclasses import dataclass

from oskar.detect.indicators import IndicatorSet
from oskar.detect.smc import SmcSnapshot

# зона близости к цене: структура интересна, если ее край в пределах N * ATR
PROXIMITY_ATR = 2.0
STRUCTURE_RECENCY_BARS = 40


@dataclass(frozen=True)
class Opportunity:
    direction: str  # long | short
    score: int  # 0-100
    components: dict[str, int]


def _near(price: float, zone_top: float, zone_bottom: float, atr: float) -> bool:
    lo, hi = zone_bottom - PROXIMITY_ATR * atr, zone_top + PROXIMITY_ATR * atr
    return lo <= price <= hi


def score_opportunity(
    snapshot: SmcSnapshot,
    ind_signal_tf: IndicatorSet,
    mtf_trend: str,
) -> Opportunity | None:
    """None = нет направленной структуры (нечего эскалировать)."""
    price = ind_signal_tf.close
    atr = ind_signal_tf.atr

    # --- направление: последнее структурное событие
    recent_structure = [
        ev
        for ev in snapshot.structure
        if snapshot.bar_index - ev.broken_index <= STRUCTURE_RECENCY_BARS
    ]
    if not recent_structure:
        return None
    last_ev = max(recent_structure, key=lambda e: e.broken_index)
    direction = "long" if last_ev.direction == "bullish" else "short"
    want = "bullish" if direction == "long" else "bearish"

    c: dict[str, int] = {}

    # --- SMC-конфлюэнс (40): FVG 15, OB 15, structure recency 10
    fvg_near = any(
        z.direction == want and _near(price, z.top, z.bottom, atr)
        for z in snapshot.active_fvgs()
    )
    c["fvg"] = 15 if fvg_near else 0

    ob_near = any(
        b.direction == want and _near(price, b.top, b.bottom, atr)
        for b in snapshot.active_order_blocks()
    )
    c["ob"] = 15 if ob_near else 0

    bars_since = snapshot.bar_index - last_ev.broken_index
    c["structure"] = 10 if bars_since <= 15 else 5

    # --- мульти-ТФ тренд (25): полный вес при согласии, 0 при противоречии
    if mtf_trend == want:
        c["trend"] = 25
    elif mtf_trend == "mixed":
        c["trend"] = 8
    else:
        c["trend"] = 0

    # --- объем (15)
    c["volume"] = 15 if ind_signal_tf.volume_ratio >= 1.5 else 0

    # --- RSI (10): не экстремум ПРОТИВ направления
    if direction == "long":
        c["rsi"] = 10 if ind_signal_tf.rsi < 60 else (0 if ind_signal_tf.rsi > 70 else 5)
    else:
        c["rsi"] = 10 if ind_signal_tf.rsi > 40 else (0 if ind_signal_tf.rsi < 30 else 5)

    # --- MACD (10): свежий кросс в сторону направления
    expected_cross = "bull_cross" if direction == "long" else "bear_cross"
    c["macd"] = 10 if ind_signal_tf.macd_cross == expected_cross else 0

    return Opportunity(direction=direction, score=sum(c.values()), components=c)
