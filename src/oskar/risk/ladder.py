"""Риск-лестница v2 — детерминированная валидация каждого торгового решения.

Порт 10-шаговой лестницы v1 + stop-bleeding фиксы + новые шаги. Порядок строг,
первый непройденный шаг останавливает оценку (но след пишется полностью до
точки останова — RiskVerdict.steps воспроизводим).

LLM не имеет канала обхода: лестница вызывается ядром между TradeDecision и
исполнением, всегда (ст. 2). Капсы читаются из caps.yaml, чей хеш сверен с
конституцией при старте демона (ст. 4).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from oskar.contracts.schemas import (
    RiskComputed,
    RiskStep,
    RiskVerdict,
    SetupCandidate,
)
from oskar.data.symbols import canonical
from oskar.risk.sizing import (
    RR_TOLERANCE,
    dynamic_leverage,
    fee_adjusted_rr,
    position_size,
)


@dataclass
class Caps:
    """Из config/caps.yaml (hash-pinned, ст. 4)."""

    risk_per_trade_pct: float
    max_total_risk_pct: float
    max_leverage: float
    confidence_cap: int
    min_rr_fee_adjusted: float
    max_open_positions: int


@dataclass
class ProfileLimits:
    """Из config/profiles.yaml активного профиля."""

    sl_dist_pct_min: float
    sl_dist_pct_max: float
    max_leverage: float


@dataclass
class OpenPosition:
    symbol: str  # канонический
    side: str  # long | short
    risk_usd: float  # риск позиции (потеря при SL-хите) — экспозиция портфеля = сумма рисков


@dataclass
class RiskInput:
    """Вход лестницы. СТРУКТУРНО не содержит производных от прошлых вердиктов
    (ст. 3): ни статистики направлений, ни прошлых RiskVerdict, ни win-rate.
    Тест test_ladder.py::test_no_feedback_fields сторожит состав полей."""

    candidate: SetupCandidate
    entry: float
    sl: float
    tp: float
    confidence: int
    equity_usd: float
    open_positions: list[OpenPosition] = field(default_factory=list)
    safe_mode: bool = False
    paused: bool = False
    context_stale: bool = False


# Поля, которым ЗАПРЕЩЕНО появляться в RiskInput (ст. 3 — анти-feedback)
FORBIDDEN_INPUT_FIELDS = {
    "portfolio_direction", "recent_win_rate", "prior_verdicts",
    "direction_stats", "performance_stats", "long_short_ratio",
}

MIN_CONFIDENCE = 60


def evaluate(inp: RiskInput, caps: Caps, profile: ProfileLimits) -> RiskVerdict:
    steps: list[RiskStep] = []
    computed: RiskComputed | None = None

    def step(name: str, passed: bool, value: str = "", limit: str = "") -> bool:
        steps.append(RiskStep(name=name, passed=passed, value=value, limit=limit))
        return passed

    def verdict(passed: bool) -> RiskVerdict:
        from oskar.ops.caps_guard import caps_hash

        return RiskVerdict(
            decision_id=inp.candidate.id,
            ts=datetime.now(UTC),
            passed=passed,
            steps=steps,
            computed=computed if passed else None,
            caps_hash=caps_hash(),
        )

    # 0. анти-feedback (структурный ассерт, ст. 3)
    leaked = FORBIDDEN_INPUT_FIELDS & set(vars(inp))
    if not step("no_feedback_fields", not leaked, value=",".join(sorted(leaked))):
        return verdict(False)

    # 1-2. глобальные блокировки
    if not step("safe_mode_off", not inp.safe_mode):
        return verdict(False)
    if not step("not_paused", not inp.paused):
        return verdict(False)

    # 3. контекст: stale НЕ блокирует (Sentinel — контекст, не альфа), только след
    step("context_freshness", True, value="stale->neutral" if inp.context_stale else "fresh")

    # 4. confidence в границах
    if not step(
        "confidence_bounds",
        MIN_CONFIDENCE <= inp.confidence <= caps.confidence_cap,
        value=str(inp.confidence),
        limit=f"[{MIN_CONFIDENCE}..{caps.confidence_cap}]",
    ):
        return verdict(False)

    # 5. SL присутствует и дистанция в рамках профиля
    sl_dist = abs(inp.entry - inp.sl)
    sl_ok = inp.sl > 0 and sl_dist > 0
    sl_dist_pct = (sl_dist / inp.entry * 100.0) if sl_ok else 0.0
    in_range = sl_ok and profile.sl_dist_pct_min <= sl_dist_pct <= profile.sl_dist_pct_max
    if not step(
        "sl_distance",
        in_range,
        value=f"{sl_dist_pct:.2f}%",
        limit=f"[{profile.sl_dist_pct_min}..{profile.sl_dist_pct_max}]%",
    ):
        return verdict(False)

    # 5b. направление согласовано с ценами (SL и TP по правильные стороны от entry)
    if inp.candidate.direction == "long":
        px_ok = inp.sl < inp.entry < inp.tp
    else:
        px_ok = inp.tp < inp.entry < inp.sl
    if not step("price_geometry", px_ok, value=f"e={inp.entry} sl={inp.sl} tp={inp.tp}"):
        return verdict(False)

    # 6. fee-aware R:R (v1 fix 1.3: до фикса фактический R:R был 0.42 при floor 1.5)
    rr = fee_adjusted_rr(inp.entry, inp.sl, inp.tp)
    if not step(
        "rr_fee_adjusted",
        rr + RR_TOLERANCE >= caps.min_rr_fee_adjusted,
        value=f"{rr:.3f}",
        limit=f">={caps.min_rr_fee_adjusted}",
    ):
        return verdict(False)

    # 7-8. леверидж и размер (только Python, ст. 2)
    lev_cap = min(profile.max_leverage, caps.max_leverage)
    leverage = dynamic_leverage(lev_cap, inp.confidence, sl_dist_pct)
    size = position_size(inp.equity_usd, caps.risk_per_trade_pct, inp.entry, inp.sl, leverage)
    step("leverage", leverage <= lev_cap, value=f"{leverage}x", limit=f"<={lev_cap}x")
    step(
        "risk_per_trade",
        True,  # размер ВЫЧИСЛЕН из капса, нарушение невозможно по построению
        value=f"${size.risk_usd:.2f}",
        limit=f"{caps.risk_per_trade_pct}% of ${inp.equity_usd:.2f}",
    )

    # 8b. осуществимость: маржа новой позиции должна помещаться в equity
    if not step(
        "margin_feasible",
        size.margin_usd < inp.equity_usd,
        value=f"${size.margin_usd:.2f}",
        limit=f"<${inp.equity_usd:.2f}",
    ):
        return verdict(False)

    # 9. суммарный риск портфеля + лимит позиций + дубли
    sym = canonical(inp.candidate.symbol)
    open_risk = sum(p.risk_usd for p in inp.open_positions)
    total_risk_pct = (open_risk + size.risk_usd) / inp.equity_usd * 100.0
    if not step(
        "portfolio_total_risk",
        total_risk_pct <= caps.max_total_risk_pct,
        value=f"{total_risk_pct:.2f}%",
        limit=f"<={caps.max_total_risk_pct}%",
    ):
        return verdict(False)
    if not step(
        "max_open_positions",
        len(inp.open_positions) < caps.max_open_positions,
        value=str(len(inp.open_positions)),
        limit=f"<{caps.max_open_positions}",
    ):
        return verdict(False)
    dup = any(
        canonical(p.symbol) == sym and p.side == inp.candidate.direction
        for p in inp.open_positions
    )
    if not step("no_duplicate_position", not dup, value=f"{sym} {inp.candidate.direction}"):
        return verdict(False)

    # 10. confluence gate (v1 fix 1.4, СИММЕТРИЧНО для обоих направлений)
    f = inp.candidate.features
    if inp.candidate.direction == "long":
        signals = [
            f.ob is not None and not f.ob.tested,
            f.fvg is not None and not f.fvg.mitigated,
            f.candle_pattern is not None,
        ]
        trend_ok = f.trend_mtf == "bullish"
        rsi_ok = f.rsi is None or f.rsi < 70
    else:
        signals = [
            f.ob is not None and not f.ob.tested,
            f.fvg is not None and not f.fvg.mitigated,
            f.candle_pattern is not None,
        ]
        trend_ok = f.trend_mtf == "bearish"
        rsi_ok = f.rsi is None or f.rsi > 30
    confluence = sum(signals) >= 2 and trend_ok and rsi_ok
    if not step(
        "confluence_gate",
        confluence,
        value=f"signals={sum(signals)}/3 trend={f.trend_mtf} rsi={f.rsi}",
        limit=">=2 signals + trend + rsi",
    ):
        return verdict(False)

    computed = RiskComputed(
        qty=size.qty,
        leverage=leverage,
        risk_usd=size.risk_usd,
        rr_fee_adjusted=round(rr, 4),
    )
    return verdict(True)
