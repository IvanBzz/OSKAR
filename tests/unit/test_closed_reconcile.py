"""Сверка закрытых сделок с филлами биржи (ст. 1 п. 1-2; ревью 2026-10-13: Claudexor H10, Codex #12)."""

import json
from datetime import UTC, datetime, timedelta

from oskar.journal.db import connect
from oskar.sync.reconcile import (
    FETCH_PARAMS,
    STATUS_CORRECTED,
    STATUS_ENTRY_UNKNOWN,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNMATCHED,
    ClosedTradeReconciler,
    parse_fill,
    reconcile_closed_trades,
    render_reconcile,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SYM = "SOL/USDT:USDT"
NOW = T0 + timedelta(hours=3)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fill(order, side, px, amt, at, *, pos="LONG", fee=0.1, cid=None, symbol=SYM, info=None):
    """ccxt trade-структура BingX swap (fetch_my_trades)."""
    raw_info = {"positionSide": pos, "orderId": order}
    if cid is not None:
        raw_info["clientOrderID"] = cid
    raw_info.update(info or {})
    return {
        "id": None, "symbol": symbol, "order": order, "side": side, "price": px, "amount": amt,
        "fee": {"cost": fee, "currency": "USDT"}, "timestamp": ms(at) if at else None, "info": raw_info,
    }


class FakeFills:
    """История исполнений биржи: since/until фильтруют по timestamp, как BingX startTs/endTs."""

    def __init__(self, fills=None, fail: Exception | None = None):
        self.fills = list(fills or [])
        self.fail = fail
        self.calls: list[tuple] = []

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        params = dict(params or {})
        self.calls.append((symbol, since, params))
        if self.fail is not None:
            raise self.fail
        until = params.get("until")
        out = []
        for f in self.fills:
            ts = f["timestamp"]
            if f["symbol"] != symbol:
                continue
            if ts is not None and ((since is not None and ts < since) or (until is not None and ts > until)):
                continue
            out.append(f)
        return out


def journal(tmp_path, entry_order_id_column=False):
    j = connect(tmp_path / "j.db")
    cols = {r["name"] for r in j.execute("PRAGMA table_info(trades)")}
    if entry_order_id_column and "entry_order_id" not in cols:  # журнал v3 уже несет колонку
        j.execute("ALTER TABLE trades ADD COLUMN entry_order_id TEXT")
        j.commit()
    return j


def add_closed(j, did, *, side="long", qty=2.0, entry=100.0, entry_fees=0.1, exit_price=None,
               exit_fees=None, pnl=None, reason="manual", created=T0, opened=T0,
               closed=T0 + timedelta(hours=1), sl=None, tp=None, cid=None, status="closed",
               reconciled=0, symbol=SYM, entry_order_id=None):
    cols = ("run_id, decision_id, client_order_id, symbol, side, status, entry_price, exit_price, qty, "
            "sl_order_id, tp_order_id, close_reason, entry_fees, exit_fees, realized_pnl, opened_at, "
            "closed_at, reconciled, created_at")
    vals = ["r1", did, cid or f"OSKAR-r1-{did}", symbol, side, status, entry, exit_price, qty,
            sl or f"{did}-sl", tp or f"{did}-tp", reason, entry_fees, exit_fees, pnl,
            iso(opened) if opened else None, iso(closed) if closed else None, reconciled, iso(created)]
    if entry_order_id is not None:
        cols += ", entry_order_id"
        vals.append(entry_order_id)
    cur = j.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    j.commit()
    return cur.lastrowid


def row(j, tid):
    return j.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()


def snapshots(j):
    return j.execute("SELECT * FROM sync_snapshots WHERE kind='reconcile' ORDER BY id").fetchall()


# ------------------------------------------------------------------ matching


def test_sl_fills_matched_by_order_id_vwap_and_fees(tmp_path):
    """SL исполнился двумя кусками: выход = VWAP по филлам ордера SL, комиссии — сумма."""
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="sl_hit", exit_price=97.95, exit_fees=0.2, pnl=-4.4)
    ex = FakeFills([
        fill("d1-sl", "sell", 98.0, 1.0, T0 + timedelta(minutes=50)),
        fill("d1-sl", "sell", 97.9, 1.0, T0 + timedelta(minutes=51)),
    ])
    rep = reconcile_closed_trades(ex, j, NOW)
    r = rep.results[0]
    assert r.status == STATUS_OK and r.method == "order_id" and r.entry_source == "journal"
    assert abs(r.exit_price - 97.95) < 1e-9 and abs(r.exit_fees - 0.2) < 1e-9
    assert abs(r.realized_pnl - (-4.4)) < 1e-9
    t = row(j, tid)
    assert t["reconciled"] == 1 and abs(t["realized_pnl"] - (-4.4)) < 1e-9
    snap = snapshots(j)
    assert len(snap) == 1 and snap[0]["clean"] == 1
    payload = json.loads(snap[0]["payload_json"])
    assert payload["trades"][0]["trade_id"] == tid and payload["trades"][0]["status"] == STATUS_OK


def test_manual_close_null_exit_filled_from_window(tmp_path):
    """manual: exit/P&L были NULL навсегда — теперь по закрывающей стороне + positionSide.
    Чужая сторона позиции и филл до входа в окно не попадают."""
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="manual")
    ex = FakeFills([
        fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30)),  # закрытие нашей LONG
        fill("x-2", "sell", 103.0, 2.0, T0 + timedelta(minutes=40), pos="SHORT"),  # открытие чужой SHORT
        fill("x-0", "sell", 90.0, 2.0, T0 - timedelta(hours=2)),  # выход прошлой позиции
    ])
    rep = reconcile_closed_trades(ex, j, NOW)
    r = rep.results[0]
    assert r.status == STATUS_OK and r.method == "window"  # NULL -> значение — не расхождение
    assert r.exit_price == 103.0 and abs(r.realized_pnl - 5.8) < 1e-9  # 3*2 - 0.1 - 0.1
    t = row(j, tid)
    assert t["exit_price"] == 103.0 and abs(t["exit_fees"] - 0.1) < 1e-12 and t["reconciled"] == 1
    assert snapshots(j)[0]["clean"] == 1


def test_short_pnl_sign_duration_timeout(tmp_path):
    j = journal(tmp_path)
    add_closed(j, "d1", side="short", qty=1.0, entry=50.0, entry_fees=0.02, reason="duration_timeout")
    ex = FakeFills([fill("c-1", "buy", 48.0, 1.0, T0 + timedelta(minutes=59), pos="SHORT", fee=0.02)])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.status == STATUS_OK and abs(r.realized_pnl - 1.96) < 1e-9  # (50-48)*1 - 0.04


def test_partial_exit_coverage_is_not_reconciled(tmp_path):
    """Филлы выхода покрыли половину qty: ничего не пишем, reconciled=0, снепшот грязный."""
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="manual")
    ex = FakeFills([fill("x-1", "sell", 103.0, 1.0, T0 + timedelta(minutes=30))])
    rep = reconcile_closed_trades(ex, j, NOW)
    r = rep.results[0]
    assert r.status == STATUS_UNMATCHED and "cover 1 of qty 2" in r.note
    t = row(j, tid)
    assert t["reconciled"] == 0 and t["exit_price"] is None and t["realized_pnl"] is None
    snap = snapshots(j)[0]
    assert snap["clean"] == 0
    assert json.loads(snap["payload_json"])["trades"][0]["status"] == STATUS_UNMATCHED


def test_excess_fills_are_ambiguous_not_reconciled(tmp_path):
    """Лишние закрывающие филлы (позиция оператора слилась с нашей) — не угадываем."""
    j = journal(tmp_path)
    add_closed(j, "d1", reason="manual")
    ex = FakeFills([
        fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30)),
        fill("x-2", "sell", 104.0, 2.0, T0 + timedelta(minutes=35)),
    ])
    assert reconcile_closed_trades(ex, j, NOW).results[0].status == STATUS_UNMATCHED


def test_client_id_case_insensitive_exact_match(tmp_path):
    """BingX вернул clientOrderID в нижнем регистре, а id ордера SL в журнале устарел
    (защиту переставляли): сделка все равно опознается точно, по clientOrderId."""
    j = journal(tmp_path)
    add_closed(j, "d1", cid="OSKAR-r1-ABC123", sl="stale-sl", reason="sl_hit",
               exit_price=97.0, exit_fees=0.1, pnl=-6.2)
    ex = FakeFills([fill("new-sl", "sell", 97.0, 2.0, T0 + timedelta(minutes=50), pos="",
                         cid="oskar-r1-abc123-sl-r1")])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.status == STATUS_OK and r.method == "order_id"


def test_exact_id_with_wrong_side_is_ignored(tmp_path):
    """Обрезанные clientOrderId старого формата могли совпасть у long и short одного символа
    (Claudexor #19): филл с «нашим» id, но чужой стороны, выходом лонга не считается."""
    j = journal(tmp_path)
    add_closed(j, "d1", cid="OSKAR-r1-SOL", reason="sl_hit", exit_price=97.0, exit_fees=0.1, pnl=-6.2)
    ex = FakeFills([
        fill("d1-sl", "sell", 97.0, 2.0, T0 + timedelta(minutes=50)),
        fill("short-tp", "buy", 95.0, 2.0, T0 + timedelta(minutes=51), pos="SHORT", cid="oskar-r1-sol-tp"),
    ])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.status == STATUS_OK and r.exit_qty == 2.0 and r.exit_price == 97.0


def test_duplicate_fill_from_exchange_counted_once(tmp_path):
    """Один и тот же филл дважды в ответе (перекрытие страниц) — не двойной выход."""
    j = journal(tmp_path)
    add_closed(j, "d1", reason="sl_hit", exit_price=97.0, exit_fees=0.1, pnl=-6.2)
    f = fill("d1-sl", "sell", 97.0, 2.0, T0 + timedelta(minutes=50))
    r = reconcile_closed_trades(FakeFills([f, dict(f)]), j, NOW).results[0]
    assert r.status == STATUS_OK and r.exit_qty == 2.0 and r.method == "order_id"


def test_journal_lie_is_corrected_and_reported(tmp_path):
    """Монитор записал цену SL вместо цены исполнения: журнал исправлен, старые числа — в diffs."""
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="sl_hit", exit_price=98.0, exit_fees=0.1, pnl=-4.2)
    ex = FakeFills([fill("d1-sl", "sell", 97.5, 2.0, T0 + timedelta(minutes=50))])
    rep = reconcile_closed_trades(ex, j, NOW)
    r = rep.results[0]
    assert r.status == STATUS_CORRECTED
    assert r.diffs["exit_price"] == [98.0, 97.5]
    assert r.diffs["realized_pnl"][0] == -4.2 and abs(r.diffs["realized_pnl"][1] - (-5.2)) < 1e-9
    t = row(j, tid)
    assert t["exit_price"] == 97.5 and t["reconciled"] == 1
    assert snapshots(j)[0]["clean"] == 0  # журнал врал — это diff для INV-003


def test_entry_order_id_column_verifies_entry(tmp_path):
    """Колонка entry_order_id есть: вход берется из филлов ордера входа; плановая цена в
    журнале (lookup входа упал) исправляется, неизвестная комиссия входа появляется."""
    j = journal(tmp_path, entry_order_id_column=True)
    tid = add_closed(j, "d1", entry=100.0, entry_fees=None, reason="manual", entry_order_id="e-1")
    ex = FakeFills([
        fill("e-1", "buy", 100.4, 2.0, T0, fee=0.08),
        fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30)),
    ])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.status == STATUS_CORRECTED and r.entry_source == "exchange"
    assert r.diffs["entry_price"] == [100.0, 100.4] and "entry_fees" not in r.diffs  # NULL -> не diff
    t = row(j, tid)
    assert t["entry_price"] == 100.4 and abs(t["entry_fees"] - 0.08) < 1e-12
    assert abs(t["realized_pnl"] - ((103.0 - 100.4) * 2 - 0.08 - 0.1)) < 1e-9


def test_entry_from_window_when_journal_incomplete(tmp_path):
    """Без entry_order_id и clientOrderId: открывающая сторона рядом с opened_at."""
    j = journal(tmp_path)
    add_closed(j, "d1", entry=100.0, entry_fees=None, reason="manual")
    ex = FakeFills([
        fill("e-1", "buy", 100.2, 2.0, T0 - timedelta(seconds=5), fee=0.08),
        fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30)),
    ])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.entry_source == "exchange_window" and r.entry_price == 100.2


def test_unknown_entry_blocks_reconciliation(tmp_path):
    """Комиссия входа неизвестна и филла входа нет: P&L без нее и был бы «выдуманным»."""
    j = journal(tmp_path)
    tid = add_closed(j, "d1", entry=100.0, entry_fees=None, reason="manual")
    ex = FakeFills([fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30))])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.status == STATUS_ENTRY_UNKNOWN
    assert row(j, tid)["reconciled"] == 0 and row(j, tid)["exit_price"] is None


def test_exchange_error_is_reported_without_signed_url(tmp_path):
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="manual")
    boom = RuntimeError("bingx GET https://open-api-vst.bingx.com/x?timestamp=1&signature=deadbeef")
    rep = reconcile_closed_trades(FakeFills(fail=boom), j, NOW)
    r = rep.results[0]
    assert r.status == STATUS_ERROR and "signature" not in r.note and "deadbeef" not in r.note
    assert row(j, tid)["reconciled"] == 0 and snapshots(j)[0]["clean"] == 0


def test_neighbour_trade_fills_do_not_leak(tmp_path):
    """Две сделки одного символа/стороны подряд: выход второй в люфте закрытия первой
    не засчитывается первой (окно режется created_at следующей), и наоборот."""
    j = journal(tmp_path)
    a = add_closed(j, "a", reason="manual", closed=T0 + timedelta(hours=1))
    b_open = T0 + timedelta(hours=1, minutes=1)
    b = add_closed(j, "b", reason="manual", created=b_open, opened=b_open,
                   closed=T0 + timedelta(hours=1, minutes=4))
    ex = FakeFills([
        fill("a-x", "sell", 101.0, 2.0, T0 + timedelta(minutes=59)),
        fill("b-x", "sell", 102.0, 2.0, T0 + timedelta(hours=1, minutes=3)),
    ])
    res = {r.trade_id: r for r in reconcile_closed_trades(ex, j, NOW).results}
    assert res[a].status == STATUS_OK and res[a].exit_price == 101.0
    assert res[b].status == STATUS_OK and res[b].exit_price == 102.0


def test_fill_owned_by_another_trade_is_excluded(tmp_path):
    """Филл без positionSide, точно принадлежащий другой сделке (ее clientOrderId),
    в окно нашей не попадает."""
    j = journal(tmp_path)
    a = add_closed(j, "a", reason="manual")
    add_closed(j, "c", side="short", status="open", reason=None, closed=None, cid="OSKAR-r1-c")
    ex = FakeFills([
        fill("a-x", "sell", 101.0, 2.0, T0 + timedelta(minutes=30), pos=""),
        fill("c-e", "sell", 101.0, 2.0, T0 + timedelta(minutes=31), pos="", cid="oskar-r1-c"),
    ])
    r = reconcile_closed_trades(ex, j, NOW).results[0]
    assert r.trade_id == a and r.status == STATUS_OK and r.exit_qty == 2.0


def test_fill_time_parsed_from_info_when_ccxt_gives_none():
    """ccxt читает 'filledTm'; если биржа отдала время только строкой в info — разбираем сами."""
    raw = fill("x-1", "sell", 103.0, 2.0, None, info={"filledTime": "2026-10-15T20:30:00.000+0800"})
    f = parse_fill(raw)
    assert f is not None and f.ts == ms(T0 + timedelta(minutes=30))


def test_query_params_window_and_units(tmp_path):
    j = journal(tmp_path)
    add_closed(j, "d1", reason="manual")
    ex = FakeFills([])
    reconcile_closed_trades(ex, j, NOW)
    symbol, since, params = ex.calls[0]
    assert symbol == SYM
    assert since == ms(T0 - timedelta(seconds=120))
    assert params["until"] == ms(T0 + timedelta(hours=1, minutes=5))
    assert params["tradingUnit"] == FETCH_PARAMS["tradingUnit"] == "COIN"


def test_dry_run_writes_nothing(tmp_path):
    j = journal(tmp_path)
    tid = add_closed(j, "d1", reason="manual")
    ex = FakeFills([fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(minutes=30))])
    rep = reconcile_closed_trades(ex, j, NOW, write=False)
    assert rep.results[0].status == STATUS_OK and rep.snapshot_id is None
    assert row(j, tid)["reconciled"] == 0 and snapshots(j) == []


def test_only_closed_unreconciled_trades_are_checked(tmp_path):
    j = journal(tmp_path)
    add_closed(j, "open", status="open", reason=None, closed=None)
    add_closed(j, "done", reconciled=1)
    ex = FakeFills([])
    rep = reconcile_closed_trades(ex, j, NOW)
    assert rep.results == [] and ex.calls == [] and rep.clean
    assert snapshots(j)[0]["clean"] == 1  # прогон без работы тоже оставляет след


def test_render_is_ascii(tmp_path):
    j = journal(tmp_path)
    add_closed(j, "d1", reason="sl_hit", exit_price=98.0, exit_fees=0.1, pnl=-4.2)
    add_closed(j, "d2", reason="manual", created=T0 + timedelta(hours=2),
               opened=T0 + timedelta(hours=2), closed=T0 + timedelta(hours=2, minutes=30))
    ex = FakeFills([fill("d1-sl", "sell", 97.5, 2.0, T0 + timedelta(minutes=50))])
    out = render_reconcile(reconcile_closed_trades(ex, j, NOW))
    out.encode("ascii")  # cp1251-урок v1
    assert "RECONCILE DIRTY" in out and "[corrected]" in out and "[unmatched]" in out


# ----------------------------------------------------------------- schedule


def test_scheduler_runs_on_new_closed_trade_then_backs_off(tmp_path):
    j = journal(tmp_path)
    sent: list[str] = []
    ex = FakeFills([])
    rec = ClosedTradeReconciler(ex, j, notify=sent.append)

    assert rec.tick(T0) is None  # закрытых нет: первый периодический прогон — через 6 ч
    tid = add_closed(j, "d1", reason="manual", closed=T0 + timedelta(minutes=1))

    t = T0 + timedelta(minutes=2)
    assert rec.tick(t) is not None  # новая закрытая несверенная — сразу
    assert rec.tick(t + timedelta(minutes=5)) is None  # повтор не чаще раза в 10 мин
    runs = 1
    for _ in range(5):
        t += timedelta(minutes=10)
        assert rec.tick(t) is not None
        runs += 1
    assert runs == 6 and len(ex.calls) == 6
    assert len(sent) == 1 and f"#{tid}" in sent[0]  # застряла — один алерт, не каждые 10 мин
    assert rec.tick(t + timedelta(minutes=30)) is None  # быстрые повторы исчерпаны
    assert rec.tick(t + timedelta(hours=6)) is not None  # дальше — только 6-часовой прогон
    assert len(sent) == 1

    ex.fills = [fill("x-1", "sell", 103.0, 2.0, T0 + timedelta(seconds=30))]
    rep = rec.tick(t + timedelta(hours=12))
    assert rep is not None and rep.results[0].status == STATUS_OK
    assert rec.pending_ids() == []
    assert rec.tick(t + timedelta(hours=12, minutes=10)) is None


def test_scheduler_periodic_run_without_trades(tmp_path):
    j = journal(tmp_path)
    rec = ClosedTradeReconciler(FakeFills([]), j)
    assert rec.tick(T0) is None
    assert rec.tick(T0 + timedelta(hours=5, minutes=59)) is None
    rep = rec.tick(T0 + timedelta(hours=6))
    assert rep is not None and rep.results == [] and len(snapshots(j)) == 1
    assert rec.tick(T0 + timedelta(hours=7)) is None


def test_scheduler_failed_run_still_counts_as_attempt(tmp_path, monkeypatch):
    """Упавший прогон не превращается в опрос биржи на каждом тике монитора."""
    import pytest

    import oskar.sync.reconcile as rc

    j = journal(tmp_path)
    add_closed(j, "d1", reason="manual")
    rec = ClosedTradeReconciler(FakeFills([]), j)

    def boom(*a, **k):
        raise RuntimeError("journal is locked")

    monkeypatch.setattr(rc, "reconcile_closed_trades", boom)
    with pytest.raises(RuntimeError):
        rec.tick(NOW)
    assert rec.tick(NOW + timedelta(minutes=1)) is None  # ждем retry_sec, а не долбим каждую минуту
    monkeypatch.undo()
    assert rec.tick(NOW + timedelta(minutes=10)) is not None


def test_cli_sync_reconcile_dry_run_wiring(tmp_path, monkeypatch):
    """`oskar sync reconcile --dry-run`: тот же read-only view, что у sync diff, журнал не пишется."""
    import sys

    import oskar.cli as cli
    import oskar.sync.exchange_sync as es
    import oskar.sync.reconcile as rc

    monkeypatch.setenv("BINGX_API_KEY", "k")
    monkeypatch.setenv("BINGX_API_SECRET", "s")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    view = FakeFills([])
    monkeypatch.setattr(es, "make_bingx_view", lambda key, secret, sandbox=True: view)
    seen = {}

    def fake_cli_reconcile(v, db, write=True):
        seen.update(v=v, db=db, write=write)
        return 0

    monkeypatch.setattr(rc, "cli_reconcile", fake_cli_reconcile)
    monkeypatch.setattr(sys, "argv", ["oskar", "sync", "reconcile", "--dry-run"])
    assert cli.main() == 0
    assert seen["v"] is view and seen["write"] is False and seen["db"].name == "oskar.db"


def test_cli_reconcile_exit_code(tmp_path, capsys):
    from oskar.sync.reconcile import cli_reconcile

    db = tmp_path / "j.db"
    j = journal(tmp_path)
    add_closed(j, "d1", reason="manual")
    j.close()
    assert cli_reconcile(FakeFills([]), db, now=NOW) == 1  # не сверилась — грязно
    assert "RECONCILE DIRTY" in capsys.readouterr().out


def test_scheduler_alerts_journal_lie_once_html_escaped(tmp_path):
    j = journal(tmp_path)
    sent: list[str] = []
    add_closed(j, "d1", reason="sl_hit", exit_price=98.0, exit_fees=0.1, pnl=-4.2)
    ex = FakeFills([fill("d1-sl", "sell", 97.5, 2.0, T0 + timedelta(minutes=50))])
    rec = ClosedTradeReconciler(ex, j, notify=sent.append)
    rec.tick(NOW)
    assert len(sent) == 1 and "журнал расходился" in sent[0] and "exit_price 98" in sent[0]
    assert "<" not in sent[0].replace("&lt;", "")  # parse_mode=HTML не ломается
