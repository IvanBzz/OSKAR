"""Резолвер counterfactuals v2 (блок R3, 2026-10-16): исходы на синтетических свечах.

Все офлайн: свечи — DataFrame, кеш — стаб. Время: SIG — open сигнального бара (в id
кандидата), окно начинается с его закрытия START = SIG + 15м.
"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from oskar.core.cf_resolver import (
    CF_RESOLVER_VERSION,
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
    assert d["entry_kind"] == "market" and d["ambiguous"] == [] and d["v"] == CF_RESOLVER_VERSION
    assert coverage_problem(res.detail_json(), res.outcome) is None


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
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(HORIZON_BARS + 5, c=101.0, h=101.5), P)
    assert res.outcome == "horizon_close" and res.detail["bars"] == HORIZON_BARS
    assert res.realized_r == pytest.approx(
        pnl_per_unit("long", 100.0, 101.0) / stop_loss_per_unit(100.0, 98.0), abs=1e-6
    )
    assert coverage_problem(res.detail_json(), res.outcome) is None


def test_duration_stop_closes_loser_after_max_hold_by_close():
    """Лосер (close ниже входа) на 12-м баре = 3 ч с входа -> закрытие по close, как монитор."""
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(30, o=99.6, h=99.8, lo=99.2, c=99.5), P)
    assert res.outcome == "duration_stop"
    assert res.detail["bars"] == 12  # 12 x 15м = 10800 с
    assert res.realized_r == pytest.approx(realized_r("long", 100.0, 98.0, 99.5), abs=1e-9)
    assert -1.0 < res.realized_r < 0


def test_winner_is_not_duration_stopped():
    res = resolve(cand("short"), 100.0, 102.0, 96.0, bars(HORIZON_BARS, c=99.5, lo=99.2), P)
    assert res.outcome == "horizon_close" and res.realized_r > 0


def test_limit_not_touched_within_ttl_is_unfilled():
    c = cand(limit=95.0)
    assert resolve(c, 95.0, 94.0, 104.0, bars(3), P) is None  # TTL (4 бара) еще не закрыт
    res = resolve(c, 95.0, 94.0, 104.0, bars(4), P)
    assert res.outcome == "unfilled" and res.realized_r == 0.0
    assert res.detail["bars"] == 4 and res.detail["fill_ms"] is None
    assert coverage_problem(res.detail_json(), res.outcome) is None
    # касание ПОСЛЕ TTL не в счет: заявку уже сняли
    late = resolve(c, 95.0, 94.0, 104.0, bars(10, at={5: {"low": 94.9}}), P)
    assert late.outcome == "unfilled"


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
    res = resolve(cand(), 100.0, 98.0, 104.0, bars(10, skip=(5,)), P)
    assert res.outcome == "no_data" and res.realized_r is None
    assert res.detail["reason"] == "gap" and res.detail["missing_ms"] == START + 5 * TF


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
    ok = {"start_ms": START, "last_bar_ms": START + 3 * TF, "bars": 4, "tf_ms": TF, "horizon_bars": 48,
          "ttl_s": 3600}
    assert coverage_problem(json.dumps(ok), "tp_hit") is None
    assert coverage_problem(None, "tp_hit") == "нет записи о покрытии"
    assert coverage_problem(json.dumps({**ok, "bars": 3}), "tp_hit")  # дыра внутри окна
    assert coverage_problem(json.dumps(ok), "horizon_close")  # горизонт не закрыт
    assert coverage_problem(json.dumps({**ok, "last_bar_ms": START + 2 * TF, "bars": 3}), "unfilled")


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
    cfg = CycleConfig(limit_entry_ttl_seconds=1800)  # 2 бара
    cycle = ShadowCycle(StubCache(bars(2)), RuntimeState(journal=j), CAPS, PROFILE, cfg)
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
