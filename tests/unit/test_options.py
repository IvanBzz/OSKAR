"""Генератор меню опций: геометрия, якоря SL, полный контракт на выходе."""

import pytest

from oskar.contracts.schemas import SetupCandidate
from oskar.detect.indicators import IndicatorSet
from oskar.detect.options import build_candidate
from oskar.detect.score import Opportunity
from oskar.detect.smc import (
    FvgZone,
    LiquidityPool,
    OrderBlock,
    SmcSnapshot,
    StructureEvent,
    Swing,
)


def ind(**overrides) -> IndicatorSet:
    base = dict(
        rsi=55.0, ema20=100.0, ema50=101.0, ema200=None, atr=1.0, atr_pct=1.0,
        macd_hist=-0.1, macd_cross="none", volume_ratio=1.8, close=100.0,
    )
    base.update(overrides)
    return IndicatorSet(**base)


def short_snapshot() -> SmcSnapshot:
    """Медвежий контекст на цене 100: FVG 101-102, OB 102-103, свинг-хай 103.5,
    пул ликвидности снизу на 95, противоположных FVG нет."""
    return SmcSnapshot(
        bar_index=100,
        detector_version="test",
        swings=[
            Swing(index=70, kind="high", level=103.5),
            Swing(index=80, kind="low", level=97.0),
        ],
        fvgs=[FvgZone(index=90, direction="bearish", top=102.0, bottom=101.0, mitigated=False)],
        order_blocks=[
            OrderBlock(index=85, direction="bearish", top=103.0, bottom=102.0,
                       volume=1.0, strength_pct=60.0, mitigated=False)
        ],
        structure=[
            StructureEvent(kind="choch", direction="bearish", swing_index=70,
                           level=97.0, broken_index=95)
        ],
        liquidity=[
            LiquidityPool(index=60, direction="bearish", level=95.0, end_index=80,
                          swept=False, swept_index=None)
        ],
    )


OPP = Opportunity(direction="short", score=85, components={})


def test_short_candidate_full_menu():
    c = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    assert isinstance(c, SetupCandidate)
    assert c.direction == "short" and c.setup_type == "confluence"

    entry_ids = {o.id for o in c.entry_options}
    assert {"e-market", "e-fvg", "e-ob"} == entry_ids
    fvg_entry = next(o for o in c.entry_options if o.id == "e-fvg")
    assert fvg_entry.price == 102.0  # верх медвежьего FVG (для short — top)

    # SL: все выше цены (short), за структурой с буфером
    for o in c.sl_options:
        assert o.price > 100.0
    swing_sl = next(o for o in c.sl_options if o.id == "s-swing")
    assert swing_sl.price == 103.5 + 0.15  # свинг + 0.15*ATR

    # TP: все ниже цены, пул ликвидности присутствует
    for o in c.tp_options:
        assert o.price < 100.0
    assert any(o.kind == "liquidity_pool" and o.price == 95.0 for o in c.tp_options)
    assert any(o.kind == "r_multiple" for o in c.tp_options)


def test_long_mirror_geometry():
    snap = SmcSnapshot(
        bar_index=100,
        detector_version="test",
        swings=[Swing(index=75, kind="low", level=96.5)],
        fvgs=[FvgZone(index=90, direction="bullish", top=99.0, bottom=98.0, mitigated=False)],
        structure=[StructureEvent(kind="bos", direction="bullish", swing_index=75,
                                  level=103.0, broken_index=95)],
    )
    c = build_candidate(
        "ETH/USDT:USDT", "1h", snap, ind(), Opportunity("long", 75, {}), "bullish"
    )
    assert c is not None and c.direction == "long"
    for o in c.entry_options:
        if o.id != "e-market":
            assert o.price < 100.0  # лимитка на откате ниже цены
    for o in c.sl_options:
        assert o.price < 100.0
    for o in c.tp_options:
        assert o.price > 100.0
    fvg_entry = next(o for o in c.entry_options if o.id == "e-fvg")
    assert fvg_entry.price == 98.0  # низ бычьего FVG


def test_no_swing_anchor_returns_none():
    """Нет подтвержденного свинга против направления — нет SL-якоря — нет сетапа."""
    snap = short_snapshot()
    no_highs = SmcSnapshot(
        bar_index=100, detector_version="test",
        swings=[s for s in snap.swings if s.kind != "high"],
        fvgs=snap.fvgs, order_blocks=snap.order_blocks,
        structure=snap.structure, liquidity=snap.liquidity,
    )
    assert build_candidate("SOL/USDT:USDT", "15m", no_highs, ind(), OPP, "bearish") is None


def test_candidate_id_deterministic():
    a = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    b = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    assert a.id == b.id == "SOLUSDTUSDT-100-short"


def test_contract_validates():
    """Выход проходит pydantic-контракт (round-trip)."""
    c = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    restored = SetupCandidate.model_validate_json(c.model_dump_json())
    assert restored == c


# ------------------------------------------------- блок F (2026-10-16): меню воронки


def _menu(c: SetupCandidate) -> dict[str, float]:
    return {o.id: o.price for o in c.entry_options + c.sl_options + c.tp_options}


def test_existing_ids_kept_and_limit_entries_get_own_atr_stops():
    """Прежние id на месте; у каждой лимитки свой ATR-стоп (s-atr от рынка при лимитке
    оказывался в 0.02-0.04% от входа)."""
    c = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    menu = _menu(c)
    assert {"e-market", "e-fvg", "e-ob", "s-swing", "s-ob", "s-atr", "t-liq", "t-r0", "t-r1"} <= set(menu)
    assert menu["s-atr"] == 100.0 + 1.5  # от рынка — как раньше
    assert menu["s-atr-fvg"] == menu["e-fvg"] + 1.5  # 1.5 ATR от своей лимитки (short — выше)
    assert menu["s-atr-ob"] == menu["e-ob"] + 1.5


def test_sl_dedup_keeps_first_option():
    """s-swing и s-ob с одной ценой (12/12 в журнале) — в меню остается s-swing."""
    snap = short_snapshot()
    same = SmcSnapshot(
        bar_index=100, detector_version="test", swings=snap.swings, fvgs=snap.fvgs,
        order_blocks=[OrderBlock(index=85, direction="bearish", top=103.5, bottom=102.5,
                                 volume=1.0, strength_pct=60.0, mitigated=False)],
        structure=snap.structure, liquidity=snap.liquidity,
    )
    c = build_candidate("SOL/USDT:USDT", "15m", same, ind(), OPP, "bearish")
    ids = [o.id for o in c.sl_options]
    assert "s-swing" in ids and "s-ob" not in ids
    prices = [o.price for o in c.sl_options]
    assert len(prices) == len(set(prices))


def test_r_targets_are_net_r_of_their_own_pair():
    """«2.0R» — ровно 2R ПОСЛЕ комиссий от своей пары вход/стоп (XAUT: подпись 2.0R,
    а с чужой парой 26.5R); каждая R-цель сочетается ровно с одной парой."""
    from oskar.detect.options import R_NET_MARGIN, combo_allowed
    from oskar.risk.sizing import fee_adjusted_rr

    c = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    for t in (o for o in c.tp_options if o.kind == "r_multiple"):
        pairs = [(e, s) for e in c.entry_options for s in c.sl_options if combo_allowed(c, e, s, t)]
        assert len(pairs) == 1, t.id
        e, s = pairs[0]
        r = 2.0 if t.id.startswith("t-r0") else 3.0
        assert fee_adjusted_rr(e.price, s.price, t.price) == pytest.approx(r + R_NET_MARGIN, abs=1e-9)
        assert t.note.startswith(f"{r}R net") and f"({e.id}/{s.id})" in t.note
        assert t.price < 100.0  # цель за рынком: иначе на исполнении «цена уже за целью»
    t_r0 = next(o for o in c.tp_options if o.id == "t-r0")
    assert "(e-market/s-swing)" in t_r0.note  # прежний id = пара (рынок, первый стоп)


def test_combo_allowed_rejects_foreign_pairs():
    from oskar.detect.options import combo_allowed

    c = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish")
    o = {x.id: x for x in c.entry_options + c.sl_options + c.tp_options}
    assert combo_allowed(c, o["e-fvg"], o["s-swing"], o["t-liq"])  # структурные — свободно
    assert combo_allowed(c, o["e-fvg"], o["s-swing"], o["t-r0-fvg-swing"])
    assert not combo_allowed(c, o["e-market"], o["s-swing"], o["t-r0-fvg-swing"])  # чужой вход
    assert not combo_allowed(c, o["e-fvg"], o["s-atr"], o["t-liq"])  # ATR-стоп рынка при лимитке
    assert not combo_allowed(c, o["e-market"], o["s-atr-fvg"], o["t-liq"])


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("price", [0.01234, 0.99, 1.9, 100.0, 3021.7, 65000.0])
def test_t_r0_passes_rr_step_for_any_stop_in_profile_range(direction, price):
    """t-r0 (2.0R net) проходит шаг R:R лестницы при любом стопе 0.5-3% — и после
    округления тейка биржей до 5 значащих цифр (запас R_NET_MARGIN + допуск гейта)."""
    import math

    from oskar.risk.ladder import Caps, ProfileLimits, RiskInput, evaluate

    caps = Caps(risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
                confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3)
    profile = ProfileLimits(sl_dist_pct_min=0.3, sl_dist_pct_max=6.0, max_leverage=8)
    sign = 1.0 if direction == "long" else -1.0
    atr = price * 0.004
    for i in range(26):
        d = 0.5 + i * 0.1  # % стопа от входа
        level = price * (1 - sign * d / 100) + sign * 0.15 * atr  # s-swing = level -/+ 0.15 ATR
        snap = SmcSnapshot(
            bar_index=100, detector_version="test",
            swings=[Swing(index=70, kind="low" if direction == "long" else "high", level=level)],
        )
        trend = "bullish" if direction == "long" else "bearish"
        c = build_candidate("SOL/USDT:USDT", "15m", snap, ind(close=price, atr=atr),
                            Opportunity(direction, 80, {}), trend)
        sl = next(o for o in c.sl_options if o.id == "s-swing").price
        tp = next(o for o in c.tp_options if o.id == "t-r0").price
        assert abs(price - sl) / price * 100 == pytest.approx(d)
        tick = 10 ** (math.floor(math.log10(tp)) - 4)
        for tp_px in (tp, round(tp / tick) * tick):
            v = evaluate(RiskInput(candidate=c, entry=price, sl=sl, tp=tp_px, confidence=68,
                                   equity_usd=1000.0), caps, profile)
            rr_step = next(s for s in v.steps if s.name == "rr_fee_adjusted")
            assert rr_step.passed, (d, tp_px, rr_step.value)
