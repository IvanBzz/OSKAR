"""Генератор меню опций: геометрия, якоря SL, полный контракт на выходе."""

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
