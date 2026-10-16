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
from oskar.risk.sizing import RR_TOLERANCE, tp_for_fee_adjusted_rr

SL_BUFFER_ATR = 0.15   # запас за структурой (не «под лой», а за уровень с люфтом — Герчик)
ATR_SL_MULT = 1.5
SWEEP_RECENCY_BARS = 20
R_MULTIPLES = (2.0, 3.0)  # в fee-adjusted R (net), как их меряет лестница
# Запас к net-R на округление цены биржей (2026-10-16): гейт прощает RR_TOLERANCE, тейк
# ставим на половину допуска выше — округление TP к входу не роняет t-r0 под кап
R_NET_MARGIN = RR_TOLERANCE / 2
SAME_PRICE_REL = 1e-6  # опции с относительной разницей цен меньше — одна и та же цена


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
    bar_ts: int | None = None,  # ms открытия последней закрытой свечи — ключ id
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
    # 2026-10-16 (блок F): ATR-стоп от КАЖДОЙ лимитной точки входа. s-atr считается от
    # рынка, и при лимитке оказывался в 0.02-0.04% от входа — размен на шум.
    for e in entries[1:]:
        e_sl = e.price - sign * ATR_SL_MULT * atr
        if _valid_sl(e_sl, e.price, direction):
            sls.append(PriceOption(id=f"s-atr-{e.id.removeprefix('e-')}", kind="atr_mult",
                                   price=e_sl, note=f"{ATR_SL_MULT}x ATR от {e.id}"))
    # 2026-10-16: s-swing совпадал по цене с s-ob в 12/12 случаев — дубль в меню
    # раздувал перебор; оставляем первую (s-swing)
    sls = _dedup_by_price(sls)

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
    # 2026-10-16 (блок F, аудит): R-тейки — для каждой пары вход/стоп и в fee-adjusted R.
    # Раньше один набор от рынка и первого стопа, валовый: «2.0R» после буфера комиссий
    # не проходил min_rr_fee_adjusted никогда, а с чужой парой выходил 26.5R (XAUT).
    # Пара (e-market, первый стоп) сохраняет прежние id t-r0/t-r1.
    for e in entries:
        for s in sls:
            if not _pair_ok(e, s) or not _valid_sl(s.price, e.price, direction):
                continue
            for i, r in enumerate(R_MULTIPLES):
                tp = tp_for_fee_adjusted_rr(e.price, s.price, r + R_NET_MARGIN)
                # тейк за рынком: иначе на исполнении «цена уже за целью»
                if tp <= 0 or not _ahead_of_market(tp, price, direction):
                    continue
                tps.append(PriceOption(id=r_tp_id(i, e.id, s.id, sls[0].id), kind="r_multiple",
                                       price=tp, note=f"{r}R net ({e.id}/{s.id})"))

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
    # Ключ бара: таймстамп открытия, если дан. bar_index в скользящем окне
    # фиксированной длины ВСЕГДА 299 -> все кандидаты символа/направления
    # получали один id (INSERT OR IGNORE молча терял решения, кулдаун не
    # истекал никогда; поймано ревью 2026-10-13: 82/82 решений с «-299-»).
    bar_key = snapshot.bar_index if bar_ts is None else bar_ts
    return SetupCandidate(
        id=f"{symbol.replace('/', '').replace(':', '')}-{bar_key}-{direction}",
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


def _ahead_of_market(tp: float, price: float, direction: str) -> bool:
    return tp > price if direction == "long" else tp < price


def _dedup_by_price(options: list[PriceOption]) -> list[PriceOption]:
    """Опции с совпадающей ценой (отн. разница < SAME_PRICE_REL) — первая остается."""
    kept: list[PriceOption] = []
    for o in options:
        if not any(abs(o.price - k.price) < SAME_PRICE_REL * max(o.price, k.price) for k in kept):
            kept.append(o)
    return kept


def _atr_anchor(sl: PriceOption) -> str | None:
    """Вход, от которого считан ATR-стоп: s-atr -> e-market, s-atr-fvg -> e-fvg."""
    if sl.kind != "atr_mult":
        return None
    return "e-market" if sl.id == "s-atr" else "e-" + sl.id.removeprefix("s-atr-")


def _pair_ok(entry: PriceOption, sl: PriceOption) -> bool:
    """ATR-стоп осмыслен только со своим входом; структурные — с любым."""
    anchor = _atr_anchor(sl)
    return anchor is None or anchor == entry.id


def r_tp_id(i: int, entry_id: str, sl_id: str, first_sl_id: str) -> str:
    """Id R-тейка пары вход/стоп. (e-market, первый стоп) — прежние t-r0/t-r1."""
    if entry_id == "e-market" and sl_id == first_sl_id:
        return f"t-r{i}"
    return f"t-r{i}-{entry_id.removeprefix('e-')}-{sl_id.removeprefix('s-')}"


def combo_allowed(candidate: SetupCandidate, entry: PriceOption, sl: PriceOption,
                  tp: PriceOption) -> bool:
    """Осмысленная ли тройка меню (2026-10-16, блок F).

    R-тейк посчитан от СВОЕЙ пары вход/стоп — с чужой «2.0R» превращался в 26.5R
    (XAUT); ATR-стоп — от своего входа. Остальные опции структурные и сочетаются
    свободно. Цены не трогает: только отсекает бессмысленные сочетания до лестницы."""
    if not _pair_ok(entry, sl):
        return False
    if tp.kind != "r_multiple":
        return True
    first = candidate.sl_options[0].id
    return tp.id in {r_tp_id(i, entry.id, sl.id, first) for i in range(len(R_MULTIPLES))}


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
