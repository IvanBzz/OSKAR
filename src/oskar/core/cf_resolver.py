"""Резолвер counterfactuals v2 — гипотетический исход сетапа по жизненному циклу живой системы.

Только измерение: торговые решения от него не зависят. Блок R3 (2026-10-16), дефекты v1
(ревью Codex, думающий центр, ночные проверки скептиков):
- нет свечей -> expired_flat. v2 требует ПОЛНОГО покрытия окна без пропусков: не хватает
  свежих баров — ждем; окно старше начала кеша или дыра в свечах — no_data, без выдумывания;
- лимитка, не коснувшаяся цены за limit_entry_ttl_seconds профиля, -> unfilled (в живой
  системе ее снимают вместе с SL/TP), R = 0;
- лосер старше max_loss_hold_seconds закрывается по рынку (duration-stop монитора) ->
  duration_stop, R по close; на горизонте HORIZON_BARS баров -> horizon_close, R по close;
- SL и TP в одной свече: SL (консервативно) + пометка ambiguous в outcome_detail;
- R после комиссий в риск-единицах сайзинга: знаменатель — sizing.stop_loss_per_unit (ход
  до стопа + проскальзывание + taker обеих ног), стоп = ровно -1R. Валовая дистанция давала
  микростопу BTC 0.017% -9.7R при -1R на счете.

Правило времени (v2): окно начинается с ЗАКРЫТИЯ сигнального бара (open из id кандидата
+ длительность ТФ; id без таймстампа — последняя граница бара <= ts). Market-вход берется
по close сигнального бара, поэтому и путь цены считается с того же момента: первый бар окна —
следующий за сигнальным (обычно в нем лежит wall-clock ts кандидата). v1 брал бары,
открывшиеся ПОСЛЕ wall-clock ts: вход по одной цене, путь — с другого момента, движение от
закрытия сигнального бара до конца бара с ts выпадало. Лимитка выставлена в тот же момент.

Полное покрытие = бары от начала окна до бара исхода включительно, подряд, без пропусков
(после выхода исход от данных не зависит). Доказательство пишется в outcome_detail
(start_ms, last_bar_ms, bars, tf_ms) — его проверяет инвариант INV-006.

Внутри бара порядок событий неизвестен, принято:
- лимитка исполнена по своей цене; бар касания — время входа (возраст для duration-stop
  считается с его открытия: стоп по времени не позже живого);
- в баре касания SL засчитывается (цена прошла вход раньше стопа), TP — нет, если бар открылся
  до уровня лимитки: тейк мог быть до входа (пометка fill_bar_tp, позиция живет дальше);
- duration-stop проверяется в закрытии бара (монитор тикает раз в 60 с: провал ниже входа
  после лимита, отыгранный внутри бара, модель не ловит — люфт до одного бара);
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
_PRICE_REL_EPS = 1e-9
_MIN_MS_TS = 10**12  # id кандидата с bar_index вместо таймстампа (до 2026-10-13) — не время


@dataclass(frozen=True)
class ResolverParams:
    """Жизненный цикл живой системы: profiles.yaml активного профиля (интрадей по умолчанию)."""

    limit_entry_ttl_seconds: int = 3600
    max_loss_hold_seconds: int = 10800
    horizon_bars: int = HORIZON_BARS


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
    """Исход одной строки по закрытым свечам ее ТФ; None — окно еще не покрыто, ждать."""
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
    if bars is None or len(bars) == 0:
        return None
    ts_col = [int(t) for t in bars["ts"]]
    first, last = min(ts_col), max(ts_col)
    if start < first:
        return _no_data("window_before_cache", start_ms=start, cache_first_ms=first)
    index = {t: i for i, t in enumerate(ts_col)}
    o, h = bars["open"].to_numpy(dtype=float), bars["high"].to_numpy(dtype=float)
    lo, c = bars["low"].to_numpy(dtype=float), bars["close"].to_numpy(dtype=float)

    kind = entry_kind(candidate, entry)
    ttl_end = start + max(1, params.limit_entry_ttl_seconds * 1000)  # бар 0 лимитке доступен всегда
    hold_ms = params.max_loss_hold_seconds * 1000
    slip = STOP_SLIPPAGE_PCT / 100.0 * entry
    filled_at = start if kind == "market" else None
    ambiguous: list[str] = []
    base = {
        "v": CF_RESOLVER_VERSION, "entry_kind": kind, "tf": tf, "tf_ms": tf_ms, "start_ms": start,
        "ts_lag_ms": ts_ms - start,  # wall-clock кандидата после закрытия сигнального бара
        "ttl_s": params.limit_entry_ttl_seconds, "hold_s": params.max_loss_hold_seconds,
        "horizon_bars": params.horizon_bars,
    }

    def done(outcome: str, k: int, exit_px: float | None) -> Resolution:
        exit_px = None if exit_px is None else float(exit_px)
        r = 0.0 if exit_px is None else realized_r(direction, entry, sl, exit_px)
        detail = {**base, "last_bar_ms": start + k * tf_ms, "bars": k + 1,
                  "fill_ms": filled_at, "exit_px": exit_px, "ambiguous": ambiguous}
        return Resolution(outcome, r, detail)

    for k in range(params.horizon_bars):
        t = start + k * tf_ms
        if filled_at is None and t >= ttl_end:
            return done("unfilled", k - 1, None)  # TTL вышел без касания: заявку сняли
        if t > last:
            return None  # бар еще не закрыт — ждем, исход не выдумываем
        i = index.get(t)
        if i is None:
            return _no_data("gap", start_ms=start, missing_ms=t)
        fill_bar = False
        if filled_at is None:
            touched = lo[i] <= entry if direction == "long" else h[i] >= entry
            if not touched:
                continue
            filled_at, fill_bar = t, True
        # позиция открыта на открытии бара — гэп за стоп исполняется по open
        open_in_pos = not fill_bar
        if direction == "long":
            sl_hit, tp_hit = lo[i] <= sl, h[i] >= tp
            gap_past_entry = o[i] <= entry
        else:
            sl_hit, tp_hit = h[i] >= sl, lo[i] <= tp
            gap_past_entry = o[i] >= entry
        if fill_bar and tp_hit and not sl_hit and not gap_past_entry:
            ambiguous.append("fill_bar_tp")  # тейк мог быть до входа: не засчитываем
            tp_hit = False
        if sl_hit:
            if tp_hit:
                ambiguous.append("sl_tp_same_bar")  # порядок неизвестен — SL первым
            trigger = sl
            if open_in_pos and sign * (o[i] - sl) <= 0:
                trigger = o[i]  # гэп за стоп: STOP_MARKET исполнится от open
            return done("sl_hit", k, trigger - sign * slip)
        if tp_hit:
            return done("tp_hit", k, tp)
        close_t = t + tf_ms
        if close_t - filled_at >= hold_ms and sign * (c[i] - entry) < 0:
            return done("duration_stop", k, c[i])
        if k == params.horizon_bars - 1:
            return done("horizon_close", k, c[i])
    # горизонт короче TTL и лимитка так и не исполнилась
    return done("unfilled", params.horizon_bars - 1, None)


def coverage_problem(detail_json: str | None, outcome: str) -> str | None:
    """Причина, по которой outcome_detail НЕ доказывает полное покрытие окна; None — доказывает.

    Инвариант INV-006: CF v2 с исходом (кроме no_data) имеют подряд bars баров от начала
    окна до бара исхода, горизонт закрыт полностью, unfilled — после конца TTL."""
    try:
        d = json.loads(detail_json or "")
        start, last, bars, tf_ms = (int(d[k]) for k in ("start_ms", "last_bar_ms", "bars", "tf_ms"))
    except (TypeError, ValueError, KeyError):
        return "нет записи о покрытии"
    if tf_ms <= 0 or bars < 1 or last < start or (last - start) % tf_ms:
        return "покрытие не по сетке ТФ"
    if (last - start) // tf_ms + 1 != bars:
        return f"баров {bars} при окне {(last - start) // tf_ms + 1}"
    if outcome == "horizon_close" and bars != int(d.get("horizon_bars") or 0):
        return "horizon_close до конца горизонта"
    if outcome == "unfilled":
        ttl_end = start + int(d.get("ttl_s") or 0) * 1000
        if last + tf_ms < min(ttl_end, start + int(d.get("horizon_bars") or 0) * tf_ms):
            return "unfilled до конца TTL"
    return None

