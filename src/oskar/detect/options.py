"""Генератор меню опций: SmcSnapshot + индикаторы -> SetupCandidate.

Ключ ст. 2: Trader НЕ пишет цены — он выбирает id из этого меню. Меню
порождается детерминированно из структур (FVG/OB/liquidity/swings) и ATR.
Опечатка модели физически не может стать кривым ордером.

Id кандидата детерминирован (symbol-bar-direction) — реплей воспроизводит.
"""

from datetime import UTC, datetime

from oskar.contracts.schemas import (
    FvgInfo,
    ObInfo,
    PriceOption,
    SetupCandidate,
    SetupFeatures,
)
from oskar.detect.indicators import IndicatorSet
from oskar.detect.score import Opportunity
from oskar.detect.smc import DETECTOR_VERSION, FvgZone, OrderBlock, SmcSnapshot

SL_BUFFER_ATR = 0.15   # запас за структурой (не «под лой», а за уровень с люфтом — Герчик)
ATR_SL_MULT = 1.5
SWEEP_RECENCY_BARS = 20
R_MULTIPLES = (2.0, 3.0)


def _nearest_zone(zones: list, price: float, want: str):
    """Ближайшая к цене активная зона нужного направления (по центру зоны)."""
    matching = [z for z in zones if z.direction == want]
    if not matching:
        return None
    return min(matching, key=lambda z: abs((z.top + z.bottom) / 2 - price))


def build_candidate(
    symbol: str,
    timeframe: str,
    snapshot: SmcSnapshot,
    ind: IndicatorSet,
    opp: Opportunity,
    mtf_trend: str,
) -> SetupCandidate | None:
    """None, если структур не хватает на осмысленное меню (нет SL-якоря)."""
    price = ind.close
    atr = ind.atr
    direction = opp.direction
    want = "bullish" if direction == "long" else "bearish"
    against = "bearish" if direction == "long" else "bullish"
    sign = 1.0 if direction == "long" else -1.0

    fvg = _nearest_zone(snapshot.active_fvgs(), price, want)
    ob = _nearest_zone(snapshot.active_order_blocks(), price, want)

    # --- SL-якорь: последний подтвержденный свинг ПРОТИВ направления
    swing_kind = "high" if direction == "short" else "low"
    anchor_swings = [s for s in snapshot.swings if s.kind == swing_kind]
    if not anchor_swings:
        return None
    last_swing = max(anchor_swings, key=lambda s: s.index)

    # --- entry options
    entries = [PriceOption(id="e-market", kind="market", price=price)]
    if fvg is not None:
        edge = fvg.bottom if direction == "long" else fvg.top
        if _valid_entry(edge, price, direction):
            entries.append(
                PriceOption(id="e-fvg", kind="limit_fvg_edge", price=edge,
                            note=f"FVG {fvg.bottom:.6g}-{fvg.top:.6g}")
            )
    if ob is not None:
        mid = (ob.top + ob.bottom) / 2
        if _valid_entry(mid, price, direction):
            entries.append(
                PriceOption(id="e-ob", kind="limit_ob_mid", price=mid,
                            note=f"OB {ob.bottom:.6g}-{ob.top:.6g}")
            )

    # --- SL options (все — ЗА структурой с люфтом, ст. знаний Герчика)
    sls = []
    swing_sl = last_swing.level - sign * SL_BUFFER_ATR * atr
    if _valid_sl(swing_sl, price, direction):
        sls.append(PriceOption(id="s-swing", kind="swing_low" if direction == "long" else "swing_high",
                               price=swing_sl, note=f"за свингом @{last_swing.level:.6g}"))
    if ob is not None:
        ob_edge = ob.bottom if direction == "long" else ob.top
        ob_sl = ob_edge - sign * SL_BUFFER_ATR * atr
        if _valid_sl(ob_sl, price, direction):
            sls.append(PriceOption(id="s-ob", kind="ob_low" if direction == "long" else "ob_high",
                                   price=ob_sl, note="за краем OB"))
    atr_sl = price - sign * ATR_SL_MULT * atr
    if _valid_sl(atr_sl, price, direction):
        sls.append(PriceOption(id="s-atr", kind="atr_mult", price=atr_sl,
                               note=f"{ATR_SL_MULT}x ATR"))
    if not sls:
        return None

    # --- TP options: ликвидность -> противоположный FVG -> R-multiples
    tps = []
    pools = [
        p for p in snapshot.intact_liquidity()
        if (p.level > price) == (direction == "long") and p.level != price
    ]
    if pools:
        nearest_pool = min(pools, key=lambda p: abs(p.level - price))
        tps.append(PriceOption(id="t-liq", kind="liquidity_pool", price=nearest_pool.level,
                               note="пул ликвидности"))
    opp_fvg = _nearest_profit_zone(snapshot.active_fvgs(), price, against, direction)
    if opp_fvg is not None:
        target = opp_fvg.bottom if direction == "long" else opp_fvg.top
        tps.append(PriceOption(id="t-fvg", kind="next_fvg", price=target,
                               note="противоположный FVG"))
    ref_sl_dist = abs(price - sls[0].price)
    for i, r in enumerate(R_MULTIPLES):
        tps.append(PriceOption(id=f"t-r{i}", kind="r_multiple",
                               price=price + sign * r * ref_sl_dist, note=f"{r}R"))

    # --- фичи
    swept = any(
        p.swept and p.swept_index is not None
        and snapshot.bar_index - p.swept_index <= SWEEP_RECENCY_BARS
        for p in snapshot.liquidity
    )
    features = SetupFeatures(
        rsi=round(ind.rsi, 2),
        atr_pct=round(ind.atr_pct, 3),
        trend_mtf=mtf_trend if mtf_trend in ("bullish", "bearish", "mixed") else None,
        fvg=_fvg_info(fvg, timeframe),
        ob=_ob_info(ob, timeframe),
        swept_liquidity=swept,
    )

    setup_type = _setup_type(fvg, ob, swept)
    return SetupCandidate(
        id=f"{symbol.replace('/', '').replace(':', '')}-{snapshot.bar_index}-{direction}",
        ts=datetime.now(UTC),
        symbol=symbol,
        direction=direction,
        setup_type=setup_type,
        timeframe=timeframe,
        score=opp.score,
        features=features,
        entry_options=entries,
        sl_options=sls,
        tp_options=tps,
        detector_versions={"oskar.detect": DETECTOR_VERSION},
    )


def _valid_entry(entry: float, price: float, direction: str) -> bool:
    """Лимитка на откате: для long — ниже цены, для short — выше."""
    if entry <= 0:
        return False
    return entry < price if direction == "long" else entry > price


def _valid_sl(sl: float, price: float, direction: str) -> bool:
    if sl <= 0:
        return False
    return sl < price if direction == "long" else sl > price


def _nearest_profit_zone(zones: list[FvgZone], price: float, want: str, direction: str):
    ahead = [
        z for z in zones
        if z.direction == want and ((z.bottom > price) if direction == "long" else (z.top < price))
    ]
    if not ahead:
        return None
    return min(ahead, key=lambda z: abs((z.top + z.bottom) / 2 - price))


def _fvg_info(fvg: FvgZone | None, timeframe: str) -> FvgInfo | None:
    if fvg is None:
        return None
    return FvgInfo(top=fvg.top, bottom=fvg.bottom, mitigated=fvg.mitigated, timeframe=timeframe)


def _ob_info(ob: OrderBlock | None, timeframe: str) -> ObInfo | None:
    if ob is None:
        return None
    return ObInfo(top=ob.top, bottom=ob.bottom, tested=ob.mitigated, timeframe=timeframe)


def _setup_type(fvg, ob, swept: bool) -> str:
    present = sum(x is not None for x in (fvg, ob)) + int(swept)
    if present >= 2:
        return "confluence"
    if fvg is not None:
        return "fvg"
    if ob is not None:
        return "ob"
    if swept:
        return "liquidity_sweep"
    return "bos"
