"""Резолвер counterfactuals v2 (блок R3, 2026-10-16): исходы на синтетических свечах.

Все офлайн: свечи — DataFrame, кеш — стаб. Время: SIG — open сигнального бара (в id
кандидата), окно начинается с его закрытия START = SIG + 15м; решение — wall-clock TS.

Ревью 2026-10-16: бары до ts не дают исхода (pre_ts, entry_stale), INV-006 сверяет число
найденных баров и время разрешения, дыра в свечах ждет подтверждения, duration-stop — по
экстремуму бара, как тики монитора.
"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from oskar.core.cf_resolver import (
    CF_RESOLVER_VERSION,
    GAP_CONFIRM_BARS,
    ResolverParams,
    coverage_problem,
    entry_kind,
    pnl_per_unit,
    realized_r,
    resolve,
    window_start_ms,
)
from oskar.core.cycle import ShadowCycle
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from oskar.journal.episodes import HORIZON_BARS, assign_episode, backfill_episodes, make_episode_id
from oskar.risk.sizing import stop_loss_per_unit
from tests.unit.test_core import CAPS, PROFILE

SYMBOL = "SOL/USDT:USDT"
TF = 900_000
SIG = 1_791_889_200_000  # 2026-10-13 11:00 UTC — граница 15м
START = SIG + TF
TS = START + 70_000  # wall-clock кандидата: 70 с после закрытия сигнального бара
P = ResolverParams(limit_entry_ttl_seconds=3600, max_loss_hold_seconds=10800)
LATER = START + 100 * TF  # resolved_at: заведомо после конца окна


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def cand(direction="long", *, limit=None, ts_ms=TS, cid=None, market=100.0):
    entries = [{"id": "e-market", "kind": "market", "price": market}]
    if limit is not None:
        entries.append({"id": "e-fvg", "kind": "limit_fvg_edge", "price": limit})
    return {
        "id": cid or f"SOLUSDTUSDT-{SIG}-{direction}", "symbol": SYMBOL, "direction": direction,
        "timeframe": "15m", "ts": iso(ts_ms), "entry_options": entries,
    }


def bars(n, *, start=START, o=100.0, h=100.5, lo=99.5, c=100.0, at=None, skip=()):
    """n баров 15м подряд от start; at={k: {open/high/low/close}} — свои значения бара k."""
    at = at or {}
    rows = []
    for k in range(n):
        if k in skip:
            continue
        b = {"ts": start + k * TF, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
        b.update(at.get(k, {}))
        rows.append(b)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ исходы


def test_tp_hit_market_long_with_net_r_and_coverage():
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(10, at={3: {"high": 104.2}}), P)
    assert res.outcome == "tp_hit"
    expected = (104.0 - 100.0 - 0.0005 * (100.0 + 104.0)) / stop_loss_per_unit(100.0, 98.0)
    assert res.realized_r == pytest.approx(expected, abs=1e-6)
    d = res.detail
    assert d["start_ms"] == START and d["last_bar_ms"] == START + 3 * TF and d["bars"] == 4
    assert d["decision_ms"] == TS and d["first_bar_ms"] == START
    assert d["entry_kind"] == "market" and d["ambiguous"] == [] and d["v"] == CF_RESOLVER_VERSION
    assert coverage_problem(res.detail_json(), res.outcome, LATER) is None


@pytest.mark.parametrize(("direction", "entry", "sl", "tp", "hit"), [
    ("long", 100.0, 98.0, 104.0, {"low": 97.9}),
    ("short", 100.0, 102.0, 96.0, {"high": 102.1}),
    ("long", 100.0, 99.983, 101.0, {"low": 99.98}),  # микростоп 0.017%: -1R, а не -9.7R
])
def test_sl_hit_is_exactly_minus_one_r(direction, entry, sl, tp, hit):
    res = resolve(cand(direction), entry, sl, tp, bars(10, h=100.004, lo=99.996, at={2: hit}), P)
    assert res.outcome == "sl_hit"
    assert res.realized_r == pytest.approx(-1.0, abs=1e-6)


def test_gap_through_stop_is_worse_than_minus_one_r():
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(5, at={2: {"open": 96.0, "low": 95.5}}), P)
    assert res.outcome == "sl_hit" and res.realized_r < -1.5
    assert res.detail["exit_px"] == pytest.approx(96.0 - 0.0005 * 100.0)


def test_sl_and_tp_in_one_bar_is_sl_marked_ambiguous():
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(5, at={1: {"high": 105.0, "low": 97.0}}), P)
    assert res.outcome == "sl_hit" and res.realized_r == pytest.approx(-1.0, abs=1e-6)
    assert res.detail["ambiguous"] == ["sl_tp_same_bar"]


def test_horizon_close_marks_to_market_by_close():
    winner = bars(HORIZON_BARS + 5, o=100.5, c=101.0, h=101.5, lo=100.2)  # ни разу не хуже входа
    res = resolve(cand(), 100.0, 98.0, 104.0, winner, P)
    assert res.outcome == "horizon_close" and res.detail["bars"] == HORIZON_BARS
    assert res.realized_r == pytest.approx(
        pnl_per_unit("long", 100.0, 101.0) / stop_loss_per_unit(100.0, 98.0), abs=1e-6
    )
    assert coverage_problem(res.detail_json(), res.outcome, LATER) is None


def test_duration_stop_loser_after_max_hold_exits_at_open():
    """Бар 12 (3 ч с входа) открылся лосером: первый тик монитора после hold -> выход по open."""
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(30, o=99.6, h=99.8, lo=99.2, c=99.5), P)
    assert res.outcome == "duration_stop"
    assert res.detail["bars"] == 13 and res.detail["last_bar_ms"] == START + 12 * TF
    assert res.detail["exit_px"] == pytest.approx(99.6)
    assert res.realized_r == pytest.approx(realized_r("long", 100.0, 98.0, 99.6), abs=1e-9)
    assert -1.0 < res.realized_r < 0 and res.detail["ambiguous"] == []


UP = {"o": 100.5, "h": 101.0, "lo": 100.2, "c": 100.8}  # победитель: ни разу не хуже входа 100


def test_duration_stop_catches_dip_inside_bar_like_monitor_ticks():
    """Проба ревью: победитель через 3 ч проваливается ниже входа ВНУТРИ бара (close выше),
    потом доходит до TP. Монитор закрыл бы его на тике провала около входа (~-0.05R);
    модель по close бара давала tp_hit +1.8R."""
    dip_then_tp = bars(30, **UP, at={13: {"low": 99.9}, 20: {"high": 104.5}})
    res = resolve(cand(), 100.0, 98.0, 104.0, dip_then_tp, P)
    assert res.outcome == "duration_stop" and res.detail["last_bar_ms"] == START + 13 * TF
    assert res.detail["exit_px"] == pytest.approx(100.0)  # бар открылся выше входа: выход ~ по входу
    assert -0.1 < res.realized_r < 0 and res.detail["ambiguous"] == []
    # провал ДО hold не стопит: монитор режет только лосера старше max_loss_hold
    early = bars(30, **UP, at={5: {"low": 99.9}, 20: {"high": 104.5}})
    assert resolve(cand(), 100.0, 98.0, 104.0, early, P).outcome == "tp_hit"


def test_duration_stop_same_bar_as_tp_sl_or_gap():
    # TP в том же баре после hold: порядок неизвестен -> duration_stop (консервативно) + пометка
    same = resolve(cand(), 100.0, 98.0, 104.0, bars(30, **UP, at={13: {"low": 99.9, "high": 104.5}}), P)
    assert same.outcome == "duration_stop" and same.detail["ambiguous"] == ["duration_tp_same_bar"]
    # SL в том же баре: цена прошла вход раньше стопа — duration_stop с пометкой (тик раз в 60 с)
    sl = resolve(cand(), 100.0, 98.0, 104.0, bars(30, **UP, at={13: {"low": 97.5}}), P)
    assert sl.outcome == "duration_stop" and sl.detail["ambiguous"] == ["duration_sl_same_bar"]
    # гэп за стоп на открытии бара после hold: STOP_MARKET на бирже раньше тика монитора
    gap = resolve(cand(), 100.0, 98.0, 104.0, bars(30, **UP, at={13: {"open": 97.0, "low": 96.5}}), P)
    assert gap.outcome == "sl_hit" and gap.realized_r < -1.0


def test_duration_stop_short_exits_at_max_of_open_and_entry():
    down = {"o": 99.5, "h": 99.8, "lo": 99.0, "c": 99.2}
    res = resolve(cand("short"), 100.0, 102.0, 96.0, bars(30, **down, at={14: {"high": 100.3}}), P)
    assert res.outcome == "duration_stop" and res.detail["exit_px"] == pytest.approx(100.0)
    opened_worse = resolve(cand("short"), 100.0, 102.0, 96.0,
                           bars(30, **down, at={14: {"open": 100.4, "high": 100.6}}), P)
    assert opened_worse.outcome == "duration_stop"
    assert opened_worse.detail["exit_px"] == pytest.approx(100.4)


def test_winner_is_not_duration_stopped():
    df = bars(HORIZON_BARS, o=99.6, h=99.9, lo=99.2, c=99.5)
    res = resolve(cand("short"), 100.0, 102.0, 96.0, df, P)
    assert res.outcome == "horizon_close" and res.realized_r > 0


def test_limit_not_touched_within_ttl_is_unfilled():
    """TTL — от решения (TS = START + 70 с): заявку не выставить раньше ts."""
    c = cand(limit=95.0)
    assert resolve(c, 95.0, 94.0, 104.0, bars(4), P) is None  # TTL до START + 70 с + 1 ч: нужен бар 4
    res = resolve(c, 95.0, 94.0, 104.0, bars(5), P)
    assert res.outcome == "unfilled" and res.realized_r == 0.0
    assert res.detail["bars"] == 5 and res.detail["fill_ms"] is None
    assert coverage_problem(res.detail_json(), res.outcome, LATER) is None
    # решение ровно на границе бара — TTL ровно 4 бара
    on_edge = resolve(cand(limit=95.0, ts_ms=START), 95.0, 94.0, 104.0, bars(4), P)
    assert on_edge.outcome == "unfilled" and on_edge.detail["bars"] == 4
    # касание ПОСЛЕ TTL не в счет: заявку уже сняли
    late = resolve(c, 95.0, 94.0, 104.0, bars(10, at={5: {"low": 94.9}}), P)
    assert late.outcome == "unfilled"


def test_limit_ttl_counts_from_decision_not_from_signal_close():
    """Лаг 10 мин: касание на баре 4 (через 60 мин от закрытия сигнального бара, но через
    50 мин от решения) — заявка еще живет."""
    c = cand(limit=95.0, ts_ms=START + 600_000)
    res = resolve(c, 95.0, 94.0, 104.0, bars(10, at={4: {"low": 94.9}, 6: {"high": 104.5}}), P)
    assert res.outcome == "tp_hit" and res.detail["fill_ms"] == START + 4 * TF


def test_limit_filled_then_tp():
    res = resolve(cand(limit=99.0), 99.0, 98.0, 103.0,
                  bars(20, at={2: {"low": 98.9}, 6: {"high": 103.5}}), P)
    assert res.outcome == "tp_hit" and res.detail["fill_ms"] == START + 2 * TF
    assert res.detail["entry_kind"] == "limit" and res.detail["bars"] == 7


def test_tp_in_fill_bar_is_not_counted():
    """Бар открылся выше лимитки, коснулся и входа, и тейка: тейк мог быть до входа."""
    c = cand(limit=99.0)
    at = {1: {"low": 98.9, "high": 103.5}}
    res = resolve(c, 99.0, 98.0, 103.0, bars(HORIZON_BARS, at=at), P)
    assert res.outcome != "tp_hit" and "fill_bar_tp" in res.detail["ambiguous"]
    again = resolve(c, 99.0, 98.0, 103.0, bars(10, at={**at, 3: {"high": 103.2}}), P)
    assert again.outcome == "tp_hit" and again.detail["ambiguous"] == ["fill_bar_tp"]
    # бар открылся уже за лимиткой: вход на открытии, тейк после него — засчитывается
    gap = resolve(c, 99.0, 98.0, 103.0, bars(5, at={1: {"open": 98.8, "low": 98.7, "high": 103.5}}), P)
    assert gap.outcome == "tp_hit" and gap.detail["ambiguous"] == []


# ------------------------------------------------------------- нет данных


def test_gap_in_candles_is_no_data_not_expired():
    """Чистая функция сообщает о дыре в текущем ответе; окончательной ее делает цикл."""
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(10, skip=(5,)), P)
    assert res.outcome == "no_data" and res.realized_r is None
    assert res.detail["reason"] == "gap" and res.detail["missing_ms"] == START + 5 * TF
    assert res.detail["cache_last_ms"] == START + 9 * TF and res.detail["tf_ms"] == TF


def test_decision_before_gap_is_still_measured():
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(10, skip=(5,), at={2: {"high": 104.5}}), P)
    assert res.outcome == "tp_hit"


def test_window_older_than_cache_is_no_data():
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(300, start=START + 2 * TF), P)
    assert res.outcome == "no_data" and res.detail["reason"] == "window_before_cache"


def test_waits_while_window_not_covered():
    assert resolve(cand(), 100.0, 98.0, 104.0, bars(5), P) is None
    assert resolve(cand(), 100.0, 98.0, 104.0, bars(0), P) is None


@pytest.mark.parametrize(("c", "levels"), [
    ({"direction": "long"}, (100.0, 98.0, 104.0)),  # нет ts
    (None, (100.0, 101.0, 104.0)),  # стоп выше входа у long
])
def test_bad_candidate_or_levels_is_no_data(c, levels):
    res = resolve(c if c is not None else cand(), *levels, bars(10), P)
    assert res.outcome == "no_data" and res.detail["reason"] in ("bad_candidate", "bad_levels")


# ------------------------------------------------------------ правило времени


def test_window_starts_at_signal_bar_close_not_after_wall_clock():
    """Market-вход — close сигнального бара: бар, в котором лежит wall-clock ts, — первый бар
    окна (v1 его пропускал); сам сигнальный бар в окно не входит."""
    df = pd.concat([
        bars(1, start=SIG, h=110.0),  # сигнальный бар: TP задет ДО входа — не в счет
        bars(10, at={0: {"low": 97.0}}),  # бар с ts: стоп
    ], ignore_index=True)
    res = resolve(cand(), 100.0, 98.0, 104.0, df, P)
    assert res.outcome == "sl_hit" and res.detail["bars"] == 1


def test_window_start_falls_back_to_bar_boundary_without_bar_ts_in_id():
    assert window_start_ms(cand(cid=f"SOLUSDTUSDT-{SIG}-long"), TS, TF) == START
    assert window_start_ms(cand(cid="SOLUSDTUSDT-299-long"), TS, TF) == START  # bar_index v1
    assert window_start_ms(cand(cid="target"), TS + 20 * 60_000, TF) == START + TF


def test_entry_kind_by_market_option():
    assert entry_kind(cand(), 100.0) == "market"
    assert entry_kind(cand(limit=99.0), 99.0) == "limit"
    assert entry_kind({"direction": "long"}, 99.0) == "market"  # меню нет — как v1


def test_coverage_problem_rejects_unproven_outcomes():
    ok = {"start_ms": START, "decision_ms": TS, "first_bar_ms": START, "last_bar_ms": START + 3 * TF,
          "bars": 4, "tf_ms": TF, "horizon_bars": 48, "ttl_s": 3600}
    assert coverage_problem(json.dumps(ok), "tp_hit", LATER) is None
    assert coverage_problem(None, "tp_hit", LATER) == "нет записи о покрытии"
    assert "найдено баров 3" in coverage_problem(json.dumps({**ok, "bars": 3}), "tp_hit", LATER)
    assert coverage_problem(json.dumps(ok), "horizon_close", LATER)  # горизонт не закрыт
    short = {**ok, "last_bar_ms": START + 2 * TF, "bars": 3}
    assert coverage_problem(json.dumps(short), "unfilled", LATER)  # раньше конца TTL
    assert coverage_problem(json.dumps(ok), "tp_hit", None) == "нет времени разрешения"


def test_coverage_counts_bars_in_candles_not_loop_index():
    """Ревью P2-1: bars = (last-start)/tf+1 из индекса цикла было тождеством. Теперь bars —
    число баров в самих свечах: если сломать проверку дыры, исход «через дыру» даст bars
    меньше окна, и INV-006 это увидит."""
    full = resolve(cand(), 100.0, 98.0, 104.0, bars(10, at={6: {"high": 104.5}}), P)
    assert full.outcome == "tp_hit" and full.detail["bars"] == 7
    with_gap = bars(10, skip=(3,), at={6: {"high": 104.5}})
    assert resolve(cand(), 100.0, 98.0, 104.0, with_gap, P).outcome == "no_data"
    # что записал бы резолвер со сломанной проверкой дыры: те же поля, bars — по свечам
    in_window = [t for t in with_gap["ts"] if START <= t <= full.detail["last_bar_ms"]]
    through_gap = {**full.detail, "bars": len(in_window)}
    assert coverage_problem(json.dumps(through_gap), "tp_hit", LATER) == "найдено баров 6 при окне 7"


def test_coverage_checks_resolution_time_by_db_clock():
    """Ревью P2-1: исход раньше закрытия бара исхода — формирующаяся свеча; horizon_close и
    unfilled раньше конца своего окна — выдумка."""
    tp = resolve(cand(), 100.0, 98.0, 104.0, bars(10, at={3: {"high": 104.5}}), P)
    closes = START + 4 * TF
    assert coverage_problem(tp.detail_json(), "tp_hit", closes) is None
    early = coverage_problem(tp.detail_json(), "tp_hit", closes - 5 * 60_000)
    assert early == "исход записан до закрытия бара исхода"
    hz = resolve(cand(), 100.0, 98.0, 104.0, bars(HORIZON_BARS, **UP), P)
    assert hz.outcome == "horizon_close"
    end = START + HORIZON_BARS * TF
    assert coverage_problem(hz.detail_json(), "horizon_close", end) is None
    assert coverage_problem(hz.detail_json(), "horizon_close", end - 10 * 60_000)
    un = resolve(cand(limit=95.0), 95.0, 94.0, 104.0, bars(5), P)
    assert un.outcome == "unfilled"
    assert coverage_problem(un.detail_json(), "unfilled", START + 5 * TF) is None
    bad = {**un.detail, "ttl_s": 7200}  # «TTL 2 ч», а исход через 1 ч — раньше конца TTL
    assert coverage_problem(json.dumps(bad), "unfilled", START + 5 * TF) == "unfilled до конца TTL"


# ------------------------------------------------- решение позже начала окна (ревью P1)


def test_bars_closed_before_decision_give_no_outcome():
    """Проба ревью: ts на 5 баров позже начала окна; TP в баре 1 и SL в баре 3 (закрылись до
    ts) — чужое прошлое, исхода не дают. Market-вход по close сигнального бара устарел: entry_stale."""
    c = cand(ts_ms=START + 5 * TF + 70_000)
    df = bars(HORIZON_BARS, o=100.2, lo=100.1, h=100.6, c=100.3,
              at={1: {"high": 104.5}, 3: {"low": 97.0}, 8: {"high": 104.2}})
    res = resolve(c, 100.0, 98.0, 104.0, df, P)
    assert res.outcome == "tp_hit" and res.detail["last_bar_ms"] == START + 8 * TF
    assert res.detail["first_bar_ms"] == START + 5 * TF and res.detail["bars"] == 4
    assert res.detail["ambiguous"] == ["entry_stale"]
    assert coverage_problem(res.detail_json(), res.outcome, LATER) is None
    # бары до решения не нужны вовсе: без них исход тот же
    tail = df[df["ts"] >= START + 5 * TF].reset_index(drop=True)
    assert resolve(c, 100.0, 98.0, 104.0, tail, P).outcome == "tp_hit"


def test_exit_in_decision_bar_is_marked_pre_ts():
    """Выход в баре, где лежит ts, мог случиться до решения: считается, но с пометкой pre_ts."""
    tp0 = resolve(cand(), 100.0, 98.0, 104.0, bars(5, at={0: {"high": 104.5}}), P)
    assert tp0.outcome == "tp_hit" and tp0.detail["ambiguous"] == ["pre_ts"]
    sl0 = resolve(cand(), 100.0, 98.0, 104.0, bars(5, at={0: {"low": 97.5}}), P)
    assert sl0.outcome == "sl_hit" and sl0.detail["ambiguous"] == ["pre_ts"]
    tp1 = resolve(cand(), 100.0, 98.0, 104.0, bars(5, at={1: {"high": 104.5}}), P)
    assert tp1.detail["ambiguous"] == []
    # решение ровно на открытии бара: до него внутри бара ничего не было
    edge = resolve(cand(ts_ms=START), 100.0, 98.0, 104.0, bars(5, at={0: {"high": 104.5}}), P)
    assert edge.outcome == "tp_hit" and edge.detail["ambiguous"] == []


def test_limit_fill_before_decision_is_ignored_and_in_decision_bar_is_pre_ts():
    c = cand(limit=99.0, ts_ms=START + 2 * TF + 70_000)
    # касание в баре 1 (закрылся до ts) не исполняет заявку: ее еще не было
    before = resolve(c, 99.0, 98.0, 103.0, bars(10, at={1: {"low": 98.9}, 6: {"high": 103.5}}), P)
    assert before.outcome == "unfilled" and before.detail["fill_ms"] is None
    # касание в баре решения — исполнение с пометкой pre_ts; лимитке entry_stale не ставится
    inside = resolve(c, 99.0, 98.0, 103.0, bars(10, at={2: {"low": 98.9}, 6: {"high": 103.5}}), P)
    assert inside.outcome == "tp_hit" and inside.detail["fill_ms"] == START + 2 * TF
    assert inside.detail["ambiguous"] == ["pre_ts"]


def test_entry_stale_only_for_market_with_lag_over_threshold():
    flat = bars(10, at={4: {"high": 104.5}})
    assert resolve(cand(ts_ms=START + 290_000), 100.0, 98.0, 104.0, flat, P).detail["ambiguous"] == []
    stale = resolve(cand(ts_ms=START + 310_000), 100.0, 98.0, 104.0, flat, P)
    assert stale.outcome == "tp_hit" and stale.detail["ambiguous"] == ["entry_stale"]
    assert stale.detail["ts_lag_ms"] == 310_000 and stale.detail["stale_s"] == 300


def test_decision_after_horizon_is_no_data():
    res = resolve(cand(ts_ms=START + HORIZON_BARS * TF + 1), 100.0, 98.0, 104.0, bars(60), P)
    assert res.outcome == "no_data" and res.detail["reason"] == "decision_after_horizon"


# ------------------------------------------------------------------ эпизоды


def test_episode_series_by_symbol_direction_and_horizon(tmp_path):
    j = connect(tmp_path / "j.db")
    hour = 3_600_000

    def add(did, direction, ts_ms):
        ep = assign_episode(j, SYMBOL, direction, ts_ms, "15m")
        j.execute(
            "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, sl_price, "
            "tp_price, episode_id) VALUES (?, 'shadow', ?, 100, 98, 104, ?)",
            (did, json.dumps({"symbol": SYMBOL, "direction": direction, "ts": iso(ts_ms),
                              "timeframe": "15m"}), ep),
        )
        return ep

    e1 = add("a", "long", TS)
    assert add("b", "long", TS + hour) == e1  # кулдаун 30 мин — та же серия
    assert add("c", "long", TS + 12 * hour) == e1  # ровно горизонт от начала серии
    e2 = add("d", "long", TS + 12 * hour + 1000)  # дальше горизонта от НАЧАЛА — новая
    assert e2 != e1 and e2 == make_episode_id(SYMBOL, "long", TS + 12 * hour + 1000)
    assert add("e", "short", TS + hour) != e1  # другое направление — своя серия
    live = dict(j.execute("SELECT decision_id, episode_id FROM counterfactuals"))
    j.execute("UPDATE counterfactuals SET episode_id = NULL")
    assert backfill_episodes(j) == 5  # миграция дает те же серии, что живая запись
    assert dict(j.execute("SELECT decision_id, episode_id FROM counterfactuals")) == live
    j.close()


# --------------------------------------------------------------- через цикл


class StubCache:
    def __init__(self, df):
        self.df = df
        self.calls = []

    def get(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return self.df


def _insert(j, did, c, entry, sl, tp, outcome=None, version=None):
    cols = "decision_id, cohort, candidate_json, entry_price, sl_price, tp_price, hypothetical_outcome"
    vals = [did, "shadow", json.dumps(c), entry, sl, tp, outcome]
    if version is not None:
        cols += ", resolver_version"
        vals.append(version)
    j.execute(f"INSERT INTO counterfactuals ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    j.commit()


def test_cycle_writes_v2_outcome_r_and_detail_and_keeps_v1_rows(tmp_path):
    j = connect(tmp_path / "j.db")
    _insert(j, "old", cand(), 100.0, 98.0, 104.0, outcome="expired_flat")  # v1: не переписывать
    _insert(j, "tp", cand(), 100.0, 98.0, 104.0, version=2)
    _insert(j, "legacy-pending", cand(cid="old-1"), 100.0, 98.0, 104.0)  # висящая строка v1
    _insert(j, "wait", cand(), 100.0, 90.0, 120.0, version=2)  # уровни далеко: ждет горизонта
    _insert(j, "ancient", cand(ts_ms=TS - 400 * TF, cid="x"), 100.0, 98.0, 104.0, version=2)
    cache = StubCache(bars(12, at={3: {"high": 104.5}}))
    cycle = ShadowCycle(cache, RuntimeState(journal=j), CAPS, PROFILE)
    assert cycle.resolve_counterfactuals(SYMBOL) == 3
    assert len(cache.calls) == 1  # один вызов кеша на символ — нагрузка как у v1
    rows = {r["decision_id"]: r for r in j.execute("SELECT * FROM counterfactuals")}
    assert rows["old"]["hypothetical_outcome"] == "expired_flat"
    assert rows["old"]["resolver_version"] == 1 and rows["old"]["realized_r"] is None
    for did in ("tp", "legacy-pending"):
        r = rows[did]
        assert r["hypothetical_outcome"] == "tp_hit" and r["resolver_version"] == 2
        assert r["realized_r"] > 0 and json.loads(r["outcome_detail"])["bars"] == 4
    assert rows["wait"]["hypothetical_outcome"] is None  # окно не покрыто — ждет
    assert rows["ancient"]["hypothetical_outcome"] == "no_data"
    assert rows["ancient"]["realized_r"] is None
    assert cycle.unresolved_symbols() == [SYMBOL]
    j.close()


def _row(j, did):
    return j.execute("SELECT * FROM counterfactuals WHERE decision_id = ?", (did,)).fetchone()


def test_cycle_gap_waits_for_confirmation_before_no_data(tmp_path):
    """Ревью P2-2: одиночный неполный ответ klines навсегда давал no_data. Теперь no_data(gap) —
    только если та же дыра осталась, пока кеш продвинулся на GAP_CONFIRM_BARS баров."""
    j = connect(tmp_path / "j.db")
    _insert(j, "hole", cand(), 100.0, 90.0, 120.0, version=2)
    cache = StubCache(bars(10, skip=(5,)))
    cycle = ShadowCycle(cache, RuntimeState(journal=j), CAPS, PROFILE)
    assert cycle.resolve_counterfactuals(SYMBOL) == 0  # первая встреча дыры — ждем
    assert cycle.resolve_counterfactuals(SYMBOL) == 0  # тот же ответ: кеш не продвинулся
    cache.df = bars(10 + GAP_CONFIRM_BARS - 1, skip=(5,))
    assert cycle.resolve_counterfactuals(SYMBOL) == 0  # продвинулся меньше, чем нужно
    assert _row(j, "hole")["hypothetical_outcome"] is None
    cache.df = bars(10 + GAP_CONFIRM_BARS, skip=(5,))
    assert cycle.resolve_counterfactuals(SYMBOL) == 1
    row = _row(j, "hole")
    detail = json.loads(row["outcome_detail"])
    assert row["hypothetical_outcome"] == "no_data" and row["realized_r"] is None
    assert detail["reason"] == "gap" and detail["missing_ms"] == START + 5 * TF
    assert detail["gap_first_seen_last_ms"] == START + 9 * TF
    j.close()


def test_cycle_gap_filled_by_fresh_response_is_measured(tmp_path):
    j = connect(tmp_path / "j.db")
    _insert(j, "hole", cand(), 100.0, 98.0, 104.0, version=2)
    _insert(j, "moved", cand(), 100.0, 90.0, 120.0, version=2)
    cache = StubCache(bars(10, skip=(2,), at={6: {"high": 104.5}}))
    cycle = ShadowCycle(cache, RuntimeState(journal=j), CAPS, PROFILE)
    assert cycle.resolve_counterfactuals(SYMBOL) == 0
    # свежий ответ без дыры в баре 2: исход меряется
    cache.df = bars(10 + GAP_CONFIRM_BARS, skip=(7,), at={6: {"high": 104.5}})
    assert cycle.resolve_counterfactuals(SYMBOL) == 1
    hole = _row(j, "hole")
    assert hole["hypothetical_outcome"] == "tp_hit" and json.loads(hole["outcome_detail"])["bars"] == 7
    # у второй строки дыра сместилась (бар 7 вместо 2): отсчет подтверждения начат заново
    assert _row(j, "moved")["hypothetical_outcome"] is None
    # окно ушло за начало кеша, пока строка ждала, — no_data(window_before_cache)
    cache.df = bars(300, start=START + 2 * TF)
    assert cycle.resolve_counterfactuals(SYMBOL) == 1
    assert json.loads(_row(j, "moved")["outcome_detail"])["reason"] == "window_before_cache"
    j.close()


def test_cycle_unknown_timeframe_is_no_data_without_exchange_call(tmp_path):
    j = connect(tmp_path / "j.db")
    _insert(j, "odd", {**cand(), "timeframe": "7m"}, 100.0, 98.0, 104.0, version=2)
    _insert(j, "notjson", cand(), 100.0, 98.0, 104.0, version=2)
    j.execute("UPDATE counterfactuals SET candidate_json = ? WHERE decision_id = 'notjson'",
              (json.dumps({"symbol": SYMBOL, "direction": "long"}),))  # без ts
    j.commit()
    cache = StubCache(bars(10))
    cycle = ShadowCycle(cache, RuntimeState(journal=j), CAPS, PROFILE)
    assert cycle.resolve_counterfactuals(SYMBOL) == 2
    assert cache.calls == [(SYMBOL, "15m", 300)]  # «7m» к бирже не ушел
    reasons = {r["decision_id"]: json.loads(r["outcome_detail"])["reason"]
               for r in j.execute("SELECT decision_id, outcome_detail FROM counterfactuals")}
    assert reasons == {"odd": "bad_candidate", "notjson": "bad_candidate"}
    j.close()


def test_cycle_uses_profile_lifecycle(tmp_path):
    from oskar.core.cycle import CycleConfig

    j = connect(tmp_path / "j.db")
    _insert(j, "lim", cand(limit=95.0), 95.0, 94.0, 104.0, version=2)
    cfg = CycleConfig(limit_entry_ttl_seconds=1800)  # 2 бара от решения (TS = START + 70 с)
    cycle = ShadowCycle(StubCache(bars(3)), RuntimeState(journal=j), CAPS, PROFILE, cfg)
    assert cycle.resolve_counterfactuals(SYMBOL) == 1
    row = j.execute("SELECT hypothetical_outcome, realized_r, outcome_detail FROM counterfactuals").fetchone()
    assert row["hypothetical_outcome"] == "unfilled" and row["realized_r"] == 0.0
    assert json.loads(row["outcome_detail"])["ttl_s"] == 1800
    j.close()


def test_shadow_cycle_insert_carries_resolver_version_and_episode(tmp_path):
    from tests.unit.test_core import make_cycle

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    report = make_cycle(state).run_once([SYMBOL], equity_usd=1000.0)
    assert report.candidates >= 1
    rows = state.journal.execute(
        "SELECT resolver_version, episode_id, candidate_json FROM counterfactuals").fetchall()
    for r in rows:
        c = json.loads(r["candidate_json"])
        assert r["resolver_version"] == CF_RESOLVER_VERSION
        assert r["episode_id"].startswith(f"{c['symbol']}|{c['direction']}|")
    state.journal.close()
