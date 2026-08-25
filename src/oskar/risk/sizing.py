"""Сайзинг и леверидж — вычисляет ТОЛЬКО Python (CONSTITUTION.md ст. 2 п. 3).

Формулы портированы из v1 (vault: dynamic-leverage.md, Phase 1 stop-bleeding):
- fee-aware R:R: из TP-дистанции вычитается 2x round-trip fee ДО расчета
- динамический леверидж = max_lev * conf_factor * vol_factor
  (v1: «потери на плохих сделках уменьшились в ~5 раз»)
- риск-базированный размер: qty = risk_usd / sl_distance
"""

from dataclasses import dataclass

ROUND_TRIP_FEE_PCT = 0.08  # % от entry за круг (открытие+закрытие, taker)
RR_TOLERANCE = 0.01        # урок v1: 1.49985 отклонялся против floor 1.5


def fee_adjusted_rr(entry: float, sl: float, tp: float) -> float:
    """R:R после вычета комиссий. Может быть отрицательным, если TP не покрывает fee."""
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist <= 0:
        raise ValueError("SL distance must be positive")
    fee_buffer = 2 * (ROUND_TRIP_FEE_PCT / 100.0) * entry
    return (tp_dist - fee_buffer) / sl_dist


def confidence_factor(confidence: int) -> float:
    """Линейно 0.3 -> 1.0 на confidence 50 -> 100; ниже 50 = 0.3."""
    if confidence <= 50:
        return 0.3
    return min(1.0, 0.3 + (confidence - 50) / 50.0 * 0.7)


def volatility_factor(sl_dist_pct: float) -> float:
    """1.5% SL-дистанции = норма; шире стоп -> меньше леверидж. Кап 1.0.

    Восстановлено по примерам v1: 60%/3.0% -> 2.2x, 90%/0.5% -> 8.6x (при max 10).
    """
    if sl_dist_pct <= 0:
        raise ValueError("sl_dist_pct must be positive")
    return min(1.0, 1.5 / sl_dist_pct)


def dynamic_leverage(max_leverage: float, confidence: int, sl_dist_pct: float) -> float:
    raw = max_leverage * confidence_factor(confidence) * volatility_factor(sl_dist_pct)
    return max(1.0, round(min(raw, max_leverage), 1))


@dataclass
class PositionSize:
    qty: float
    leverage: float
    risk_usd: float
    margin_usd: float
    notional_usd: float


def position_size(
    equity_usd: float,
    risk_pct: float,
    entry: float,
    sl: float,
    leverage: float,
) -> PositionSize:
    """Риск-базированный размер: потеря при SL-хите = risk_pct от equity."""
    if equity_usd <= 0:
        raise ValueError("equity must be positive")
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        raise ValueError("SL distance must be positive")
    risk_usd = equity_usd * risk_pct / 100.0
    qty = risk_usd / sl_dist
    notional = qty * entry
    margin = notional / leverage
    return PositionSize(
        qty=qty, leverage=leverage, risk_usd=risk_usd,
        margin_usd=margin, notional_usd=notional,
    )
