"""ops/invariants.py — вердикты ст. 5 по журналу (ревью 2026-10-13, Codex #27)."""

from datetime import UTC, datetime, timedelta

from oskar.journal.db import connect
from oskar.ops.invariants import (
    ERROR,
    GREEN,
    INSUFFICIENT,
    METRICS,
    OVERHOLD_GRACE_SEC,
    RED,
    Context,
    cli_main,
    evaluate,
    exit_code,
    load_invariants,
    write_verdict_note,
)
from oskar.sync.reconcile import reconcile_closed_trades

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LIMIT = 10800  # max_loss_hold_seconds интрадей-профиля


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def add(j, did, *, reason="sl_hit", pnl=-1.0, duration=600, closed=NOW - timedelta(hours=1),
        status="closed", entry=100.0, entry_fees=0.1, qty=1.0, reconciled=0):
    opened = closed - timedelta(seconds=duration) if duration is not None else None
    cur = j.execute(
        "INSERT INTO trades (run_id, decision_id, client_order_id, symbol, side, status, entry_price, "
        "qty, close_reason, entry_fees, realized_pnl, opened_at, closed_at, duration_seconds, "
        "reconciled, created_at, sl_order_id, tp_order_id) "
        "VALUES ('r1',?,?,?,'long',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, f"OSKAR-r1-{did}", "SOL/USDT:USDT", status, entry, qty, reason, entry_fees, pnl,
         iso(opened) if opened else iso(closed), iso(closed), duration, reconciled,
         iso(opened) if opened else iso(closed), f"{did}-sl", f"{did}-tp"),
    )
    j.commit()
    return cur.lastrowid


def inv(metric, window, expect="== 0", iid="INV-T"):
    return {"id": iid, "metric": metric, "window": window, "expect": expect, "description": "test"}


def one(j, invariant, now=NOW):
    return evaluate(j, now, invariants=[invariant], max_loss_hold_seconds=LIMIT)[0]


def test_every_declared_metric_is_implemented():
    """Ст. 5 п. 1: инвариант обязан быть машинно-проверяемым — имя метрики без функции = error."""
    declared = load_invariants()
    assert declared, "config/invariants.yaml пуст"
    missing = [i["metric"] for i in declared if i["metric"] not in METRICS]
    assert not missing, f"metrics without implementation: {missing}"


def test_context_reads_active_profile_limit():
    from oskar.core.bootstrap import load_active_profile

    assert Context().loss_hold_limit() == load_active_profile().max_loss_hold_seconds


# ------------------------------------------------------------- INV-001


def test_close_reason_full_window_green(tmp_path):
    j = connect(tmp_path / "j.db")
    for i in range(20):
        add(j, f"d{i}", closed=NOW - timedelta(hours=i + 1))
    v = one(j, inv("close_reason_unknown_count", {"closed_trades": 20}))
    assert v.status == GREEN and v.value == 0 and v.n == 20


def test_short_window_is_not_green(tmp_path):
    """5 сделок из 20 и ни одного нарушения — это insufficient_data, а НЕ green."""
    j = connect(tmp_path / "j.db")
    for i in range(5):
        add(j, f"d{i}")
    v = one(j, inv("close_reason_unknown_count", {"closed_trades": 20}))
    assert v.status == INSUFFICIENT and "5 из 20" in v.detail


def test_upper_bound_violation_is_red_even_on_short_window(tmp_path):
    j = connect(tmp_path / "j.db")
    add(j, "ok")
    bad = add(j, "bad", reason=None)
    v = one(j, inv("close_reason_unknown_count", {"closed_trades": 20}))
    assert v.status == RED and v.value == 1 and f"#{bad}" in v.detail


def test_lower_bound_on_short_window_stays_insufficient(tmp_path):
    """'>= 5' на неполном окне: нехватка — не нарушение, данных просто мало."""
    j = connect(tmp_path / "j.db")
    add(j, "d0", reason=None)
    v = one(j, inv("close_reason_unknown_count", {"closed_trades": 20}, expect=">= 5"))
    assert v.status == INSUFFICIENT


def test_hours_window_excludes_old_trades(tmp_path):
    j = connect(tmp_path / "j.db")
    add(j, "old", reason=None, closed=NOW - timedelta(hours=72))
    add(j, "fresh", closed=NOW - timedelta(hours=2))
    v = one(j, inv("close_reason_unknown_count", {"hours": 48}))
    assert v.status == GREEN and v.n == 1
    empty = connect(tmp_path / "e.db")
    assert one(empty, inv("close_reason_unknown_count", {"hours": 48})).status == INSUFFICIENT


# ------------------------------------------------------------- INV-002


def test_loser_overhold_red(tmp_path):
    j = connect(tmp_path / "j.db")
    tid = add(j, "slow", pnl=-3.0, duration=LIMIT + OVERHOLD_GRACE_SEC + 60, reconciled=1)
    v = one(j, inv("loser_overhold_count", {"closed_trades": 20}))
    assert v.status == RED and v.value == 1 and f"#{tid}" in v.detail


def test_duration_stop_within_grace_winners_any_age(tmp_path):
    """Duration-stop закрывает на тике после лимита — люфт; победитель может жить сколько угодно."""
    j = connect(tmp_path / "j.db")
    add(j, "stop", reason="duration_timeout", pnl=-2.0, duration=LIMIT + 90, reconciled=1)
    add(j, "winner", reason="tp_hit", pnl=5.0, duration=LIMIT * 4, reconciled=1)
    v = one(j, inv("loser_overhold_count", {"hours": 48}))
    assert v.status == GREEN and v.value == 0


def test_unknown_pnl_long_trade_is_insufficient(tmp_path):
    """manual-закрытие до сверки: P&L NULL, жила дольше лимита — лосер ли, неизвестно."""
    j = connect(tmp_path / "j.db")
    for i in range(20):
        add(j, f"d{i}", pnl=1.0, closed=NOW - timedelta(hours=i + 2))
    add(j, "manual", reason="manual", pnl=None, duration=LIMIT * 2)
    v = one(j, inv("loser_overhold_count", {"closed_trades": 20}))
    assert v.status == INSUFFICIENT and "не классифицировать" in v.detail


# ------------------------------------------------------------- INV-003


class Fills:
    def __init__(self, fills):
        self.fills = fills

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        return list(self.fills)


def _sl_fill(did, px, at):
    return {"order": f"{did}-sl", "side": "sell", "price": px, "amount": 1.0,
            "fee": {"cost": 0.05}, "timestamp": int(at.timestamp() * 1000), "info": {"positionSide": "LONG"}}


def test_reconcile_diff_rows_needs_reconciler_evidence(tmp_path):
    """Сделка без результата сверки (в т.ч. reconciled=1 самим монитором) — не доказательство."""
    j = connect(tmp_path / "j.db")
    add(j, "d1", reconciled=1)
    v = one(j, inv("reconcile_diff_rows", {"hours": 48}))
    assert v.status == INSUFFICIENT and "не сверены" in v.detail
    empty = connect(tmp_path / "e.db")
    assert one(empty, inv("reconcile_diff_rows", {"hours": 48})).status == INSUFFICIENT


def test_reconcile_diff_rows_green_after_clean_reconcile(tmp_path):
    j = connect(tmp_path / "j.db")
    closed = NOW - timedelta(hours=2)
    # P&L монитора совпадает с биржей: (99 - 100) * 1 - 0.1 - 0.05
    add(j, "d1", pnl=-1.15, closed=closed)
    j.execute("UPDATE trades SET exit_price=99.0, exit_fees=0.05")
    j.commit()
    reconcile_closed_trades(Fills([_sl_fill("d1", 99.0, closed - timedelta(seconds=30))]), j,
                            NOW - timedelta(hours=1))
    v = one(j, inv("reconcile_diff_rows", {"hours": 48}))
    assert v.status == GREEN and v.value == 0 and v.n == 1


def test_reconcile_diff_rows_red_when_journal_lied(tmp_path):
    j = connect(tmp_path / "j.db")
    closed = NOW - timedelta(hours=2)
    add(j, "d1", pnl=-1.0, closed=closed)  # журнал: -1.0, биржа: -2.15
    reconcile_closed_trades(Fills([_sl_fill("d1", 98.0, closed - timedelta(seconds=30))]), j,
                            NOW - timedelta(hours=1))
    v = one(j, inv("reconcile_diff_rows", {"hours": 48}))
    assert v.status == RED and v.value == 1 and "corrected" in v.detail


def test_reconcile_latest_result_wins(tmp_path):
    """Сначала филлы еще не доехали (unmatched), следующий прогон сверил — расхождения нет."""
    j = connect(tmp_path / "j.db")
    closed = NOW - timedelta(hours=2)
    add(j, "d1", reason="manual", pnl=None, closed=closed)
    reconcile_closed_trades(Fills([]), j, NOW - timedelta(minutes=100))
    assert one(j, inv("reconcile_diff_rows", {"hours": 48})).status == RED
    reconcile_closed_trades(Fills([_sl_fill("d1", 99.0, closed - timedelta(seconds=30))]), j,
                            NOW - timedelta(minutes=90))
    assert one(j, inv("reconcile_diff_rows", {"hours": 48})).status == GREEN


# ------------------------------------------------------------- errors & note


def test_unimplemented_metric_or_bad_spec_is_error(tmp_path):
    j = connect(tmp_path / "j.db")
    verdicts = evaluate(j, NOW, max_loss_hold_seconds=LIMIT, invariants=[
        inv("no_such_metric", {"hours": 1}, iid="INV-A"),
        inv("close_reason_unknown_count", {"days": 3}, iid="INV-B"),
        inv("close_reason_unknown_count", {"hours": 1}, expect="zero", iid="INV-C"),
    ])
    assert [v.status for v in verdicts] == [ERROR, ERROR, ERROR]
    assert "not implemented" in verdicts[0].detail
    assert exit_code(verdicts) == 1


def test_verdict_note_named_by_conclusion(tmp_path):
    j = connect(tmp_path / "j.db")
    add(j, "bad", reason=None)
    verdicts = evaluate(j, NOW, max_loss_hold_seconds=LIMIT, invariants=[
        inv("close_reason_unknown_count", {"closed_trades": 20}, iid="INV-001"),
        inv("loser_overhold_count", {"closed_trades": 20}, iid="INV-002"),
    ])
    runs = tmp_path / "runs"
    p1 = write_verdict_note(verdicts, NOW, runs)
    p2 = write_verdict_note(verdicts, NOW, runs)
    assert p1.name == "инварианты 2026-10-15 12-00 UTC — красный INV-001, без данных INV-002.md"
    assert p2.name.endswith("(2).md") and p1.exists()
    assert not set(p1.name) & set(':*?"<>|')  # имя валидно для Windows
    text = p1.read_text(encoding="utf-8")
    assert text.startswith("---\ntags: [runs, invariants, validation]\ndate: 2026-10-15\nstatus: red\n---\n")
    assert "| INV-001 | close_reason_unknown_count |" in text and "[[changelog]]" in text


def test_cli_invariants_wiring(monkeypatch):
    import sys

    import oskar.cli as cli
    import oskar.ops.invariants as oi

    seen = {}
    monkeypatch.setattr(oi, "cli_main", lambda db, write_note=True: seen.update(db=db, note=write_note) or 1)
    monkeypatch.setattr(sys, "argv", ["oskar", "invariants", "--no-note"])
    assert cli.main() == 1  # red -> exit 1 доходит до процесса
    assert seen["note"] is False and seen["db"].name == "oskar.db"


def test_cli_main_exit_codes(tmp_path, capsys):
    db = tmp_path / "j.db"
    j = connect(db)
    add(j, "ok")
    j.close()
    spec = [inv("close_reason_unknown_count", {"closed_trades": 20}, iid="INV-001")]
    assert cli_main(db, write_note=False, now=NOW, invariants=spec) == 0  # insufficient — не провал
    assert "INSUFFICIENT_DATA" in capsys.readouterr().out

    j = connect(db)
    add(j, "bad", reason=None)
    j.close()
    runs = tmp_path / "runs"
    assert cli_main(db, now=NOW, runs_dir=runs, invariants=spec) == 1
    assert len(list(runs.glob("*.md"))) == 1


def test_unreconciled_pnl_never_proves_green(tmp_path):
    """Перепроверка Codex: 20 несверенных «прибыльных» сделок дольше лимита давали green —
    P&L монитора не факт, пока сверка по филлам его не подтвердила."""
    j = connect(tmp_path / "j.db")
    for i in range(20):
        add(j, f"w{i}", reason="manual", pnl=5.0, duration=LIMIT * 2, reconciled=0)
    v = one(j, inv("loser_overhold_count", {"closed_trades": 20}))
    assert v.status == INSUFFICIENT
