"""Риск-лестница: таблично каждый шаг, edge cases v1, анти-feedback сторож."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from oskar.contracts.schemas import (
    FvgInfo,
    ObInfo,
    PriceOption,
    SetupCandidate,
    SetupFeatures,
)
from oskar.core.escalation import build_plan
from oskar.risk import ladder
from oskar.risk.ladder import (
    FORBIDDEN_INPUT_FIELDS,
    Caps,
    OpenPosition,
    ProfileLimits,
    RiskInput,
    evaluate,
)
from oskar.risk.sizing import PositionSize

CAPS = Caps(
    risk_per_trade_pct=1.0,
    max_total_risk_pct=5.0,
    max_leverage=10,
    confidence_cap=75,
    min_rr_fee_adjusted=2.0,
    max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.5, sl_dist_pct_max=3.0, max_leverage=8)


def features_ok(direction: str = "short") -> SetupFeatures:
    tf = "15m"
    return SetupFeatures(
        rsi=55.0,
        trend_mtf="bearish" if direction == "short" else "bullish",
        fvg=FvgInfo(top=101.0, bottom=100.0, mitigated=False, timeframe=tf),
        ob=ObInfo(top=102.0, bottom=101.0, tested=False, timeframe=tf),
    )


def candidate(direction: str = "short", **overrides) -> SetupCandidate:
    base = dict(
        id="d-001",
        ts=datetime.now(UTC),
        symbol="SOL/USDT:USDT",
        direction=direction,
        setup_type="fvg",
        timeframe="15m",
        score=78,
        features=features_ok(direction),
        entry_options=[PriceOption(id="e1", kind="limit_fvg_edge", price=100.0)],
        sl_options=[PriceOption(id="s1", kind="swing_low", price=101.5)],
        tp_options=[PriceOption(id="t1", kind="liquidity_pool", price=96.0)],
    )
    base.update(overrides)
    return SetupCandidate(**base)


def good_input(**overrides) -> RiskInput:
    base = dict(
        candidate=candidate(),
        entry=100.0,
        sl=101.5,  # 1.5% SHORT
        tp=96.0,   # rr_fee = (4 - 0.20) / 1.5 = 2.53
        confidence=68,
        equity_usd=1000.0,
    )
    base.update(overrides)
    return RiskInput(**base)


def failed_step(verdict):
    return next((s.name for s in verdict.steps if not s.passed), None)


def stop_loss_usd(qty: float, entry: float, sl: float) -> float:
    """Независимый пересчет потери на стопе: исполнение хуже SL на 0.05% от entry,
    taker BingX 0.05% на вход и на выход (каждая нога от своей цены)."""
    exit_px = sl - 0.0005 * entry if sl < entry else sl + 0.0005 * entry
    return qty * abs(entry - exit_px) + qty * 0.0005 * (entry + exit_px)


def test_happy_path_passes():
    v = evaluate(good_input(), CAPS, PROFILE)
    assert v.passed, [s for s in v.steps if not s.passed]
    assert v.computed is not None
    assert v.computed.risk_usd == pytest.approx(10.0)
    assert v.computed.leverage <= 8
    assert len(v.caps_hash) == 64


def test_safe_mode_blocks():
    v = evaluate(good_input(safe_mode=True), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "safe_mode_off"


def test_paused_blocks():
    v = evaluate(good_input(paused=True), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "not_paused"


def test_stale_context_does_not_block():
    """Sentinel — контекст, не альфа: stale деградирует, но не останавливает."""
    v = evaluate(good_input(context_stale=True), CAPS, PROFILE)
    assert v.passed
    ctx = next(s for s in v.steps if s.name == "context_freshness")
    assert ctx.value == "stale->neutral"


def test_confidence_below_min_blocks():
    v = evaluate(good_input(confidence=59), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "confidence_bounds"


def test_sl_too_tight_blocks():
    v = evaluate(good_input(sl=100.3, tp=99.0), CAPS, PROFILE)  # 0.3% < 0.5%
    assert not v.passed and failed_step(v) == "sl_distance"


def test_sl_too_wide_blocks():
    v = evaluate(good_input(sl=104.0, tp=90.0), CAPS, PROFILE)  # 4% > 3%
    assert not v.passed and failed_step(v) == "sl_distance"


def test_wrong_price_geometry_blocks():
    """SHORT с TP выше entry — геометрия сломана."""
    v = evaluate(good_input(tp=103.0), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "price_geometry"


def test_rr_below_floor_blocks():
    # SHORT: SL 101.5 (1.5), TP 97.5 (2.5): rr = (2.5-0.20)/1.5 = 1.53 < 2.0
    v = evaluate(good_input(tp=97.5), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "rr_fee_adjusted"


def test_rr_float_tolerance_v1_bug():
    """Урок v1: R:R на границе с плавающей точкой не должен резаться.
    Подбираем TP так, чтобы rr_fee = 1.9999.. — проходит с tolerance 0.01."""
    # rr = (tp_dist - 0.20) / 1.5 = 1.995 -> tp_dist = 3.1925
    v = evaluate(good_input(tp=100.0 - 3.1925), CAPS, PROFILE)
    assert v.passed, failed_step(v)


def test_total_risk_cap_blocks():
    """Открытые позиции уже несут $45 риска (4.5%); новая с $10 пробивает 5%."""
    positions = [OpenPosition("BTC/USDT:USDT", "long", risk_usd=45.0)]
    v = evaluate(good_input(open_positions=positions), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "portfolio_total_risk"


def test_margin_infeasible_blocks():
    """Леверидж 1x + узкий стоп 0.5%: маржа ~1.5x equity — не помещается."""
    no_lev = ProfileLimits(sl_dist_pct_min=0.4, sl_dist_pct_max=3.0, max_leverage=1)
    v = evaluate(good_input(sl=100.5, tp=97.5), CAPS, no_lev)  # SHORT, SL 0.5%
    assert not v.passed and failed_step(v) == "margin_feasible"


# --- ревью 2026-10-13: Codex #20 (целое плечо) и #21 (издержки в риск-капе) ---


def test_leverage_is_whole_number_sent_to_exchange():
    """8x * conf 68 * SL 1.5% = 4.416: раньше в вердикт шло 4.4x, а BingX получал 4x."""
    v = evaluate(good_input(), CAPS, PROFILE)
    assert v.passed, failed_step(v)
    assert v.computed.leverage == 4
    lev_step = next(s for s in v.steps if s.name == "leverage")
    assert lev_step.value == "4x"
    margin = next(s for s in v.steps if s.name == "margin_feasible")
    assert margin.value == f"${v.computed.qty * 100.0 / 4:.2f}"  # notional / ЦЕЛОЕ плечо


def test_margin_checked_on_integer_leverage():
    """Кап 3x * conf 75 = 1.95x: на 1.95x маржа $789 влезла бы в $1000, но на биржу
    уйдет 1x — маржа $1538 не помещается. Проверяется то, что будет отправлено."""
    profile = ProfileLimits(sl_dist_pct_min=0.4, sl_dist_pct_max=3.0, max_leverage=3)
    v = evaluate(good_input(sl=100.5, tp=97.5, confidence=75), CAPS, profile)
    assert not v.passed and failed_step(v) == "margin_feasible"
    assert next(s for s in v.steps if s.name == "leverage").value == "1x"


def test_leverage_step_stops_ladder():
    """Непройденный шаг «leverage» раньше писался в след, но лестница шла дальше и
    давала passed=True. Кап < 1x: минимальное целое плечо 1x — отказ."""
    sub_one = ProfileLimits(sl_dist_pct_min=0.5, sl_dist_pct_max=3.0, max_leverage=0.5)
    v = evaluate(good_input(), CAPS, sub_one)
    assert not v.passed and failed_step(v) == "leverage"


def test_risk_per_trade_includes_costs_codex_scenario():
    """Codex #21: $1000, 1%, LONG entry 100, SL 99.5 -> было qty 20 и на стопе
    $10 хода + ~$3 издержек. Теперь потеря С комиссиями и проскальзыванием = $10."""
    inp = good_input(candidate=candidate(direction="long"), sl=99.5, tp=102.0)
    v = evaluate(inp, CAPS, PROFILE)
    assert v.passed, failed_step(v)
    assert v.computed.qty == pytest.approx(15.391, abs=1e-3)
    loss = stop_loss_usd(v.computed.qty, 100.0, 99.5)
    assert loss == pytest.approx(10.0) and v.computed.risk_usd == pytest.approx(loss)
    step = next(s for s in v.steps if s.name == "risk_per_trade")
    assert "издержки" in step.value and step.limit.startswith("<=$10.00")


def test_risk_per_trade_rejects_fee_blind_sizing(monkeypatch):
    """Шаг больше не `True` по построению: сайзинг, забывший издержки (старая формула
    qty = risk / |entry - SL|), отклоняется — потеря на стопе выше капа."""

    def legacy(equity_usd, risk_pct, entry, sl, leverage):
        risk = equity_usd * risk_pct / 100.0
        qty = risk / abs(entry - sl)
        return PositionSize(qty=qty, leverage=leverage, risk_usd=risk,
                            margin_usd=qty * entry / leverage, notional_usd=qty * entry)

    monkeypatch.setattr(ladder, "position_size", legacy)
    v = evaluate(good_input(), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "risk_per_trade"


def test_plan_money_fields_come_from_verdict():
    """build_plan: qty/leverage/risk_usd — из вердикта как есть. Плечо целое,
    risk_usd — потеря на стопе С издержками от входа, проверенного лестницей."""
    v = evaluate(good_input(), CAPS, PROFILE)
    plan = build_plan(candidate(), v, SimpleNamespace(tldr=""), 100.0, 101.5, 96.0, "limit")
    assert (plan.qty, plan.leverage, plan.risk_usd) == (
        v.computed.qty, v.computed.leverage, v.computed.risk_usd)
    assert plan.leverage == int(plan.leverage)
    assert plan.risk_usd == pytest.approx(stop_loss_usd(plan.qty, 100.0, 101.5))
    assert plan.risk_usd <= 10.0 * (1 + 1e-9)


def test_max_positions_blocks():
    positions = [
        OpenPosition("BTC/USDT:USDT", "long", 1.0),
        OpenPosition("ETH/USDT:USDT", "long", 1.0),
        OpenPosition("ADA/USDT:USDT", "short", 1.0),
    ]
    v = evaluate(good_input(open_positions=positions), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "max_open_positions"


def test_duplicate_position_blocked_across_formats():
    """Сценарий бага v1: открыта ADA-USDT SHORT, кандидат ADA/USDT:USDT SHORT."""
    positions = [OpenPosition("ADA-USDT", "short", 1.0)]
    v = evaluate(
        good_input(candidate=candidate(symbol="ADA/USDT:USDT"), open_positions=positions),
        CAPS,
        PROFILE,
    )
    assert not v.passed and failed_step(v) == "no_duplicate_position"


def test_confluence_gate_blocks_weak_setup():
    weak = features_ok("short")
    weak.fvg = None
    weak.ob = None  # 0 из 3 сигналов
    v = evaluate(good_input(candidate=candidate(features=weak)), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "confluence_gate"


def test_confluence_gate_blocks_counter_trend():
    ct = features_ok("short")
    ct.trend_mtf = "bullish"  # SHORT против тренда
    v = evaluate(good_input(candidate=candidate(features=ct)), CAPS, PROFILE)
    assert not v.passed and failed_step(v) == "confluence_gate"


def test_confluence_symmetric_for_long():
    """v2: гейт симметричен (v1 закрыл только LONG, SHORT так и не зеркалили)."""
    c = candidate(direction="long")
    v = evaluate(
        good_input(candidate=c, entry=100.0, sl=98.5, tp=104.0), CAPS, PROFILE
    )
    assert v.passed, failed_step(v)


def test_no_feedback_fields():
    """Ст. 3: RiskInput структурно не содержит производных от прошлых вердиктов."""
    actual_fields = set(RiskInput.__dataclass_fields__)
    assert not (FORBIDDEN_INPUT_FIELDS & actual_fields)
    for banned in ["portfolio_direction", "recent_win_rate", "direction_stats"]:
        assert banned not in actual_fields


def test_verdict_trace_is_complete():
    """След лестницы содержит все пройденные шаги до точки останова."""
    v = evaluate(good_input(confidence=59), CAPS, PROFILE)
    names = [s.name for s in v.steps]
    assert names == [
        "no_feedback_fields", "safe_mode_off", "not_paused",
        "context_freshness", "confidence_bounds",
    ]
