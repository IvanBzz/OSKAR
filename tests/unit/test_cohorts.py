"""`oskar cohorts` (блок R3, 2026-10-16): сводка когорт только чтением журнала."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from oskar.journal.db import connect
from oskar.ops.cohorts import (
    CfRow,
    bootstrap_ci,
    cli_main,
    first_of_episodes,
    load_rows,
    open_readonly,
    summarize,
)

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add(j, did, *, cohort="shadow", outcome=None, r=None, version=2, at=T0, episode=None,
        ambiguous=False, direction="long", flags=None):
    if flags is None:
        flags = ["sl_tp_same_bar"] if ambiguous else []
    detail = json.dumps({"ambiguous": flags})
    j.execute(
        "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, sl_price, tp_price, "
        "hypothetical_outcome, resolver_version, realized_r, outcome_detail, episode_id) "
        "VALUES (?,?,?,100,98,104,?,?,?,?,?)",
        (did, cohort, json.dumps({"symbol": "SOL/USDT:USDT", "direction": direction, "ts": iso(at)}),
         outcome, version, r, detail, episode),
    )


@pytest.fixture
def journal(tmp_path):
    db = tmp_path / "j.db"
    j = connect(db)
    ep1 = "SOL/USDT:USDT|long|" + iso(T0)
    ep2 = "SOL/USDT:USDT|long|" + iso(T0 + timedelta(days=1))
    # v2 shadow: эпизод 1 — три строки серии (первая — tp), эпизод 2 — sl; unfilled; no_data; висящая
    add(j, "a1", outcome="tp_hit", r=2.0, episode=ep1)
    add(j, "a2", outcome="tp_hit", r=2.0, at=T0 + timedelta(minutes=30), episode=ep1)
    add(j, "a3", outcome="sl_hit", r=-1.0, at=T0 + timedelta(hours=1), episode=ep1, ambiguous=True)
    add(j, "b1", outcome="sl_hit", r=-1.0, at=T0 + timedelta(days=1), episode=ep2)
    add(j, "c1", outcome="unfilled", r=0.0, at=T0 + timedelta(days=2))
    add(j, "d1", outcome="no_data", at=T0 + timedelta(days=2, hours=1))
    add(j, "e1", at=T0 + timedelta(days=3))
    # v1: исходы без R
    add(j, "o1", outcome="tp_hit", version=1)
    add(j, "o2", outcome="expired_flat", version=1)
    add(j, "x1", cohort="executed", outcome="horizon_close", r=0.5)
    j.commit()
    j.close()
    return db


def test_summary_by_cohort_and_version(journal):
    conn = open_readonly(journal)
    groups = {(g.cohort, g.version): g for g in summarize(load_rows(conn), n_boot=200)}
    conn.close()
    g = groups[("shadow", 2)]
    assert (g.n, g.resolved, g.pending, g.ambiguous) == (7, 6, 1, 1)
    assert g.counts == {"tp_hit": 2, "sl_hit": 2, "unfilled": 1, "no_data": 1}
    # по сделкам: R есть у 5 (unfilled = 0R, no_data и висящая — без R)
    assert g.trades.n == 5 and g.trades.mean == pytest.approx((2 + 2 - 1 - 1 + 0) / 5)
    # по эпизодам: первая строка серии ep1 (tp +2), ep2 (-1), c1 (0), d1/e1 без R
    assert g.n_episodes == 5 and g.episodes.n == 3
    assert g.episodes.mean == pytest.approx((2 - 1 + 0) / 3)
    assert g.trades.blocks == 3 and g.trades.ci is not None
    lo, hi = g.trades.ci
    assert lo <= g.trades.mean <= hi
    old = groups[("shadow", 1)]
    assert old.counts == {"tp_hit": 1, "expired_flat": 1} and old.trades.n == 0
    assert groups[("executed", 2)].trades.ci is None  # одни сутки — CI не считается


def test_bootstrap_resamples_whole_days_and_is_reproducible():
    blocks = {"d1": [1.0, 1.0, 1.0], "d2": [-1.0], "d3": [0.5, -0.5]}
    a = bootstrap_ci(blocks, n_boot=500, seed=7)
    assert a == bootstrap_ci(blocks, n_boot=500, seed=7)
    assert -1.0 <= a[0] <= a[1] <= 1.0
    # сутки целиком: среднее ресэмпла — всегда смесь целых блоков, а не отдельных строк
    assert bootstrap_ci({"d1": [1.0, 1.0], "d2": [1.0]}, n_boot=50) == (1.0, 1.0)
    assert bootstrap_ci({"d1": [1.0, 2.0]}) is None


def test_episode_representative_is_first_by_time_not_by_outcome():
    rows = [
        CfRow(2, "shadow", 2, "sl_hit", -1.0, "ep", 2000, "2026-10-14", False),
        CfRow(1, "shadow", 2, "tp_hit", 2.0, "ep", 1000, "2026-10-14", False),
        CfRow(3, "shadow", 2, None, None, "ep2", 500, "2026-10-14", False),
    ]
    firsts = {x.episode: x.id for x in first_of_episodes(rows)}
    assert firsts == {"ep": 1, "ep2": 3}


def test_cli_is_read_only_and_reads_v4_journal(tmp_path, capsys):
    """Журнал до миграции (v4) читается как есть: версия 1, без R; файл не мигрирует."""
    from tests.unit.test_journal import _v4_journal

    db = tmp_path / "v4.db"
    _v4_journal(db)
    before = db.read_bytes()
    assert cli_main(db, n_boot=50) == 0
    out = capsys.readouterr().out
    assert "shadow v1:" in out and "resolver v1 has no R" in out
    assert out.isascii()
    assert db.read_bytes() == before
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    raw.close()
    with pytest.raises(sqlite3.OperationalError):  # mode=ro: запись невозможна
        conn = open_readonly(db)
        try:
            conn.execute("DELETE FROM counterfactuals")
        finally:
            conn.close()


def test_cli_render_v2_lines(journal, capsys):
    assert cli_main(journal, n_boot=100) == 0
    out = capsys.readouterr().out
    assert "shadow v2: n=7 resolved=6 pending=1 episodes=5 ambiguous=1" in out
    assert "tp_hit 33.3% | sl_hit 33.3% | unfilled 16.7% | no_data 16.7%" in out
    assert "R per trade  : n=5 mean R +0.400" in out and "R per episode: n=3 mean R +0.333" in out


def test_pre_decision_rows_get_separate_r_line(tmp_path, capsys):
    """Ревью 2026-10-16 (P1): строки pre_ts/entry_stale — исход мог взять цену до решения —
    считаются в общей строке R и дополнительно показывается строка R без них."""
    db = tmp_path / "j.db"
    j = connect(db)
    add(j, "p1", outcome="tp_hit", r=2.0, flags=["pre_ts"])
    add(j, "p2", outcome="tp_hit", r=1.5, at=T0 + timedelta(days=1), flags=["entry_stale"])
    add(j, "c1", outcome="sl_hit", r=-1.0, at=T0 + timedelta(days=2))
    add(j, "c2", outcome="tp_hit", r=1.0, at=T0 + timedelta(days=3), flags=["sl_tp_same_bar"])
    j.commit()
    j.close()
    conn = open_readonly(db)
    (g,) = summarize(load_rows(conn), n_boot=100)
    conn.close()
    assert g.flags == {"pre_ts": 1, "entry_stale": 1, "sl_tp_same_bar": 1} and g.ambiguous == 3
    assert g.trades.n == 4 and g.trades.mean == pytest.approx(3.5 / 4)
    assert g.clean_trades.n == 2 and g.clean_trades.mean == pytest.approx(0.0)  # c1, c2
    assert g.clean_episodes.n == 2
    assert cli_main(db, n_boot=100) == 0
    out = capsys.readouterr().out
    assert "ambiguous=3 (entry_stale 1, pre_ts 1, sl_tp_same_bar 1)" in out
    assert "R per trade   w/o pre_ts,entry_stale: n=2 mean R +0.000" in out
    assert "R per episode w/o pre_ts,entry_stale: n=2" in out and out.isascii()


def test_cli_missing_journal(tmp_path, capsys):
    assert cli_main(tmp_path / "nope.db") == 1
    assert "journal not found" in capsys.readouterr().out
