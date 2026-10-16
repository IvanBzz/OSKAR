"""Резолвер counterfactuals v2 — гипотетический исход сетапа по жизненному циклу живой системы.

Только измерение: торговые решения от него не зависят. Блок R3 (2026-10-16), дефекты v1
(ревью Codex, думающий центр, ночные проверки скептиков):
- нет свечей -> expired_flat. v2 требует ПОЛНОГО покрытия окна без пропусков: не хватает
  свежих баров — ждем; окно старше начала кеша — no_data; дыра в свечах — no_data, только
  если цикл подтвердил ее за GAP_CONFIRM_BARS новых баров кеша (без выдумывания);
- лимитка, не коснувшаяся цены за limit_entry_ttl_seconds профиля, -> unfilled (в живой
  системе ее снимают вместе с SL/TP), R = 0;
- лосер старше max_loss_hold_seconds закрывается по рынку (duration-stop монитора) ->
  duration_stop; на горизонте HORIZON_BARS баров -> horizon_close, R по close;
- SL и TP в одной свече: SL (консервативно) + пометка ambiguous в outcome_detail;
- R после комиссий в риск-единицах сайзинга: знаменатель — sizing.stop_loss_per_unit (ход
  до стопа + проскальзывание + taker обеих ног), стоп = ровно -1R. Валовая дистанция давала
  микростопу BTC 0.017% -9.7R при -1R на счете.

Правило времени (v2, ревью 2026-10-16). Горизонт начинается с ЗАКРЫТИЯ сигнального бара
(open из id кандидата + длительность ТФ; id без таймстампа — последняя граница бара <= ts):
к нему относится цена market-входа (close сигнального бара). Но решение принято в
wall-clock ts кандидата (лаг p50 63 с, p95 837 с, до 898 с), и движение до ts — чужое прошлое:
исполнитель такие сетапы отклоняет («цена уже за целью/стопом», «план устарел»), а v2 до
ревью засчитывал им tp_hit. Поэтому:
- бары, закрывшиеся до ts, исхода не дают (ни выхода, ни касания лимитки);
- выход или касание лимитки в баре, содержащем ts, — пометка ambiguous pre_ts (часть бара
  была до решения);
- market-вход при лаге больше stale_entry_seconds — пометка entry_stale (цена входа устарела);
- TTL лимитки отсчитывается от ts (заявку не выставить раньше решения).

Полное покрытие = бары от бара решения до бара исхода включительно, подряд, без пропусков
(после выхода исход от данных не зависит). Доказательство в outcome_detail: start_ms,
decision_ms, first_bar_ms, last_bar_ms, tf_ms и bars — число баров, РЕАЛЬНО найденных в свечах
между first_bar_ms и last_bar_ms (не индекс цикла). Его и время разрешения перепроверяет INV-006.

Внутри бара порядок событий неизвестен, принято:
- лимитка исполнена по своей цене; бар касания — время входа (возраст для duration-stop
  считается с его открытия; market — с открытия бара решения: стоп по времени не позже живого);
- в баре касания SL засчитывается (цена прошла вход раньше стопа), TP — нет, если бар открылся
  до уровня лимитки: тейк мог быть до входа (пометка fill_bar_tp, позиция живет дальше);
- duration-stop: монитор закрывает лосера на ЛЮБОМ 60-секундном тике после hold при
  unrealized_pnl < 0 (core/position_monitor.py). Поэтому в баре после hold, где экстремум против
  позиции пересек вход, — выход по min(open, entry) для long / max для short (бар открылся
  лосером — по open, иначе цена прошла вход). TP или SL в том же баре — пометка ambiguous
  (duration_tp_same_bar / duration_sl_same_bar): цена пересекает вход раньше стопа, но монитор
  тикает раз в 60 с;
- гэп за стоп (бар открылся за SL при открытой позиции) — выход по open с проскальзыванием,
  R хуже -1; гэп за тейк — выход по TP (консервативно).
"""

import json
from dataclasses import dataclass, field

import pandas as pd

from oskar.journal.episodes import DEFAULT_TIMEFRAME, HORIZON_BARS, TIMEFRAME_MS, iso_to_ms
from oskar.risk.sizing import STOP_SLIPPAGE_PCT, TAKER_FEE_PCT, stop_loss_per_unit

CF_RESOLVER_VERSION = 2
# исходы, для которых v2 утверждает полное покрытие окна (no_data — честное «не измерено»)
COVERED_OUTCOMES = ("tp_hit", "sl_hit", "unfilled", "duration_stop", "horizon_close")
# ревью 2026-10-16: дыра в свечах становится no_data, только если пережила столько новых баров
# кеша (свежих ответов klines) — одиночный неполный ответ не хоронит строку навсегда
GAP_CONFIRM_BARS = 2
# ревью 2026-10-16: часы БД и биржи расходятся на секунды — допуск проверки «исход после
# закрытия бара исхода» (формирующаяся свеча ловится, кроме последних 30 с ее жизни)
CLOCK_SKEW_MS = 30_000
_PRICE_REL_EPS = 1e-9
_MIN_MS_TS = 10**12  # id кандидата с bar_index вместо таймстампа (до 2026-10-13) — не время


@dataclass(frozen=True)
class ResolverParams:
    """Жизненный цикл живой системы: profiles.yaml активного профиля (интрадей по умолчанию)."""

    limit_entry_ttl_seconds: int = 3600
    max_loss_hold_seconds: int = 10800
    horizon_bars: int = HORIZON_BARS
    # ревью 2026-10-16: market-вход по close сигнального бара, решение позже на лаг; > 5 мин
    # (29% строк журнала 16.10) цена входа уже не та, что видел бы исполнитель -> entry_stale
    stale_entry_seconds: int = 300


@dataclass(frozen=True)
class Resolution:
    outcome: str
    realized_r: float | None
    detail: dict = field(default_factory=dict)

    def detail_json(self) -> str:
        return json.dumps(self.detail, sort_keys=True, separators=(",", ":"))


def _same_price(a: float, b: float) -> bool:
    return abs(a - b) <= _PRICE_REL_EPS * max(1.0, abs(a), abs(b))


def entry_kind(candidate: dict, entry: float) -> str:
    """market — цена входа равна market-опции меню (или меню нет); иначе limit."""
    market_px = next(
        (o.get("price") for o in candidate.get("entry_options") or [] if o.get("kind") == "market"),
        None,
    )
    if market_px is None or _same_price(float(entry), float(market_px)):
        return "market"
    return "limit"


def window_start_ms(candidate: dict, ts_ms: int, tf_ms: int) -> int:
    """Закрытие сигнального бара: open из id '<SYM>-<bar_ts>-<dir>' + ТФ, иначе граница <= ts."""
    try:
        bar_ts = int(str(candidate.get("id", "")).rsplit("-", 2)[1])
    except (IndexError, ValueError):
        bar_ts = None
    if bar_ts is not None and _MIN_MS_TS <= bar_ts <= ts_ms:
        return bar_ts + tf_ms
    return (ts_ms // tf_ms) * tf_ms


def pnl_per_unit(direction: str, entry: float, exit_px: float) -> float:
    """P&L на 1 единицу qty после taker-комиссий обеих ног (каждая от своей цены), USDT."""
    move = exit_px - entry if direction == "long" else entry - exit_px
    return move - TAKER_FEE_PCT / 100.0 * (entry + exit_px)


def realized_r(direction: str, entry: float, sl: float, exit_px: float) -> float:
    """R в риск-единицах сайзинга OSKAR: стоп с проскальзыванием и комиссиями = -1R."""
    return round(float(pnl_per_unit(direction, entry, exit_px) / stop_loss_per_unit(entry, sl)), 6)


def _no_data(reason: str, **extra) -> Resolution:
    return Resolution("no_data", None, {"v": CF_RESOLVER_VERSION, "reason": reason, **extra})


def resolve(
    candidate: dict, entry: float, sl: float, tp: float, bars: pd.DataFrame,
    params: ResolverParams | None = None,
) -> Resolution | None:
    """Исход одной строки по закрытым свечам ее ТФ; None — окно еще не покрыто, ждать.

    no_data с reason=gap — дыра в ТЕКУЩЕМ ответе свечей: окончательной ее делает цикл, когда
    дыра пережила GAP_CONFIRM_BARS новых баров кеша (ShadowCycle.resolve_counterfactuals)."""
    params = params or ResolverParams()
    try:
        direction = candidate["direction"]
        ts_ms = iso_to_ms(candidate["ts"])
        tf = candidate.get("timeframe") or DEFAULT_TIMEFRAME
        tf_ms = TIMEFRAME_MS[tf]
        if direction not in ("long", "short"):
            raise ValueError(direction)
    except (KeyError, TypeError, ValueError):
        return _no_data("bad_candidate")
    entry, sl, tp = float(entry), float(sl), float(tp)
    sign = 1.0 if direction == "long" else -1.0
    if not (entry > 0 and sl > 0 and tp > 0 and sign * (entry - sl) > 0 and sign * (tp - entry) > 0):
        return _no_data("bad_levels")  # стоп/тейк не по свою сторону входа: R не определен

    start = window_start_ms(candidate, ts_ms, tf_ms)
    decision = max(start, ts_ms)  # момент решения: раньше него ни выхода, ни заявки
    k0 = (decision - start) // tf_ms  # бар, в котором лежит решение; бары до него — прошлое
    if k0 >= params.horizon_bars:
        return _no_data("decision_after_horizon", start_ms=start, decision_ms=decision)
    first_bar = start + k0 * tf_ms
    ts_inside_bar = decision > first_bar  # часть бара решения прошла ДО ts
    if bars is None or len(bars) == 0:
        return None
    ts_col = [int(t) for t in bars["ts"]]
    first, last = min(ts_col), max(ts_col)
    if first_bar < first:
        return _no_data("window_before_cache", start_ms=start, first_bar_ms=first_bar,
                        cache_first_ms=first)
    index = {t: i for i, t in enumerate(ts_col)}
    o, h = bars["open"].to_numpy(dtype=float), bars["high"].to_numpy(dtype=float)
    lo, c = bars["low"].to_numpy(dtype=float), bars["close"].to_numpy(dtype=float)

    kind = entry_kind(candidate, entry)
    ttl_end = decision + max(1, params.limit_entry_ttl_seconds * 1000)  # бар решения доступен всегда
    hold_ms = params.max_loss_hold_seconds * 1000
    slip = STOP_SLIPPAGE_PCT / 100.0 * entry
    filled_at = first_bar if kind == "market" else None
    ambiguous: list[str] = []
    if kind == "market" and ts_ms - start > params.stale_entry_seconds * 1000:
        ambiguous.append("entry_stale")
    base = {
        "v": CF_RESOLVER_VERSION, "entry_kind": kind, "tf": tf, "tf_ms": tf_ms, "start_ms": start,
        "decision_ms": decision, "first_bar_ms": first_bar,
        "ts_lag_ms": ts_ms - start,  # wall-clock кандидата после закрытия сигнального бара
        "ttl_s": params.limit_entry_ttl_seconds, "hold_s": params.max_loss_hold_seconds,
        "horizon_bars": params.horizon_bars, "stale_s": params.stale_entry_seconds,
    }

    def flag(name: str) -> None:
        if name not in ambiguous:
            ambiguous.append(name)

    def done(outcome: str, k: int, exit_px: float | None) -> Resolution:
        exit_px = None if exit_px is None else float(exit_px)
        r = 0.0 if exit_px is None else realized_r(direction, entry, sl, exit_px)
        last_bar = start + k * tf_ms
        # ревью 2026-10-16 (INV-006): число баров считается по самим свечам, не по индексу цикла —
        # сломанная проверка дыры дала бы bars меньше окна, и инвариант это увидит
        found = len({t for t in ts_col if first_bar <= t <= last_bar})
        detail = {**base, "last_bar_ms": last_bar, "bars": found,
                  "fill_ms": filled_at, "exit_px": exit_px, "ambiguous": ambiguous}
        return Resolution(outcome, r, detail)

    for k in range(k0, params.horizon_bars):
        t = start + k * tf_ms
        if filled_at is None and t >= ttl_end:
            return done("unfilled", k - 1, None)  # TTL вышел без касания: заявку сняли
        if t > last:
            return None  # бар еще не закрыт — ждем, исход не выдумываем
        i = index.get(t)
        if i is None:
            return _no_data("gap", start_ms=start, missing_ms=t, cache_last_ms=last, tf_ms=tf_ms)
        decision_bar = ts_inside_bar and k == k0
        fill_bar = False
        if filled_at is None:
            touched = lo[i] <= entry if direction == "long" else h[i] >= entry
            if not touched:
                continue
            filled_at, fill_bar = t, True
            if decision_bar:
                flag("pre_ts")  # касание могло быть до выставления заявки
        # позиция открыта на открытии бара — гэп за стоп исполняется по open
        open_in_pos = not fill_bar
        if direction == "long":
            sl_hit, tp_hit = lo[i] <= sl, h[i] >= tp
            gap_past_entry = o[i] <= entry
            against_entry = lo[i] < entry  # unrealized_pnl < 0 хоть на одном тике бара
        else:
            sl_hit, tp_hit = h[i] >= sl, lo[i] <= tp
            gap_past_entry = o[i] >= entry
            against_entry = h[i] > entry
        if fill_bar and tp_hit and not sl_hit and not gap_past_entry:
            flag("fill_bar_tp")  # тейк мог быть до входа: не засчитываем
            tp_hit = False
        if decision_bar and (sl_hit or tp_hit):
            flag("pre_ts")  # выход мог случиться до решения
        gap_through_sl = open_in_pos and sign * (o[i] - sl) <= 0
        if not fill_bar and t >= filled_at + hold_ms and against_entry and not gap_through_sl:
            # duration-stop монитора: первый тик после hold с ценой хуже входа
            if tp_hit:
                flag("duration_tp_same_bar")
            if sl_hit:
                flag("duration_sl_same_bar")
            exit_px = min(o[i], entry) if direction == "long" else max(o[i], entry)
            return done("duration_stop", k, exit_px)
        if sl_hit:
            if tp_hit:
                flag("sl_tp_same_bar")  # порядок неизвестен — SL первым
            trigger = o[i] if gap_through_sl else sl  # гэп за стоп: STOP_MARKET исполнится от open
            return done("sl_hit", k, trigger - sign * slip)
        if tp_hit:
            return done("tp_hit", k, tp)
        if k == params.horizon_bars - 1:
            return done("horizon_close", k, c[i])
    # горизонт короче TTL и лимитка так и не исполнилась
    return done("unfilled", params.horizon_bars - 1, None)


def coverage_problem(detail_json: str | None, outcome: str, resolved_at_ms: int | None) -> str | None:
    """Причина, по которой outcome_detail НЕ доказывает полное покрытие окна; None — доказывает.

    Инвариант INV-006: CF v2 с исходом (кроме no_data) имеют подряд bars баров от бара решения
    до бара исхода (bars резолвер считает по самим свечам), исход записан после закрытия бара
    исхода по часам БД (resolved_at: формирующаяся свеча не в счет), horizon_close и unfilled —
    не раньше конца своего окна (горизонт / TTL от решения)."""
    try:
        d = json.loads(detail_json or "")
        start, first_bar, last, bars, tf_ms, decision = (int(d[k]) for k in (
            "start_ms", "first_bar_ms", "last_bar_ms", "bars", "tf_ms", "decision_ms"))
    except (TypeError, ValueError, KeyError, AttributeError):
        return "нет записи о покрытии"
    if tf_ms <= 0 or not start <= first_bar <= last:
        return "покрытие не по сетке ТФ"
    if (first_bar - start) % tf_ms or (last - first_bar) % tf_ms:
        return "покрытие не по сетке ТФ"
    window = (last - first_bar) // tf_ms + 1
    if bars != window:
        return f"найдено баров {bars} при окне {window}"
    if resolved_at_ms is None:
        return "нет времени разрешения"
    if resolved_at_ms + CLOCK_SKEW_MS < last + tf_ms:
        return "исход записан до закрытия бара исхода"
    # конец окна у horizon_close/unfilled — из параметров, не из last_bar_ms: вместе с проверкой
    # выше это дает resolved_at >= конца окна
    horizon_end = start + int(d.get("horizon_bars") or 0) * tf_ms
    if outcome == "horizon_close" and last + tf_ms != horizon_end:
        return "horizon_close до конца горизонта"
    if outcome == "unfilled" and last + tf_ms < min(decision + int(d.get("ttl_s") or 0) * 1000, horizon_end):
        return "unfilled до конца TTL"
    return None
