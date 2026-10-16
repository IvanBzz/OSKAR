"""Блок I2/I3 (2026-10-16): approve через API (Hermes) исполняет тем же охраняемым
исполнителем, что кнопки и автомат, и отвечает фактическим исходом; GET /positions и /pnl;
скрипты скиллов oskar-approvals / oskar-control."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from oskar.core.api import build_app
from oskar.core.approvals import approve_and_execute, default_entry_type
from oskar.core.executor import (
    KIND_ENTRY_UNKNOWN,
    KIND_REFUSED,
    ExecutionOutcome,
    equity_guarded,
)
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from tests.unit.test_buttons import add_pending
from tests.unit.test_executor import make_service, plan
from tests.unit.test_position_monitor import add_trade

REPO = Path(__file__).resolve().parents[2]


def _load_script(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolution(j, decision_id):
    return j.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id=?", (decision_id,)
    ).fetchone()["resolution"]


def make_api(tmp_path, execute_fn, **kw):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    done = []
    app = build_app(state, execute_fn=execute_fn, on_card_done=done.append, **kw)
    return TestClient(app), state, done


# -------------------------------------------------------------- сквозной путь


def test_api_approve_runs_the_guarded_executor_end_to_end(tmp_path):
    """Hermes approve -> equity_guarded(ExecutionService.execute) -> сделка в журнале; ответ —
    исход исполнителя, кнопки с карточки сняты."""
    svc, j, adapter, p = make_service(tmp_path)
    state = RuntimeState(journal=j)
    done = []
    client = TestClient(build_app(
        state, execute_fn=equity_guarded(svc.execute, lambda: "exchange"), on_card_done=done.append,
    ))
    r = client.post(f"/decisions/{p.candidate_id}/approve", params={"entry": "market"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, body
    assert body["kind"] == "ok" and body["entry_type"] == "market" and body["entry_sent"] is True
    assert "маркет" in body["note"] and body["resolution"] == "approved"
    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert done == [p.candidate_id]
    # повторный approve — 409, вторая позиция невозможна
    assert client.post(f"/decisions/{p.candidate_id}/approve").status_code == 409


def test_api_approve_refused_by_same_checks_keeps_card_alive(tmp_path):
    """Те же проверки, что у кнопок: без equity с биржи — отказ, approve откатан, карточка
    жива (раньше: «взято в работу», а карточка потом отвечала 409 — сетап терялся)."""
    svc, j, adapter, p = make_service(tmp_path)
    source = ["default"]
    state = RuntimeState(journal=j)
    done = []
    client = TestClient(build_app(
        state, execute_fn=equity_guarded(svc.execute, lambda: source[0]), on_card_done=done.append,
    ))
    body = client.post(f"/decisions/{p.candidate_id}/approve").json()
    assert body["ok"] is False and body["kind"] == KIND_REFUSED and body["entry_sent"] is False
    assert "equity" in body["note"] and body["resolution"] is None
    assert resolution(j, p.candidate_id) is None and done == []
    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    source[0] = "exchange"  # биржа ответила — то же решение можно принять снова
    assert client.post(f"/decisions/{p.candidate_id}/approve").json()["ok"] is True


# ---------------------------------------------------------------- исходы


def test_entry_sent_failure_keeps_approved(tmp_path):
    out = ExecutionOutcome(False, "биржа не ответила на вход", entry_sent=True, kind=KIND_ENTRY_UNKNOWN)
    client, state, done = make_api(tmp_path, lambda d, m: out)
    add_pending(state.journal, "d1")
    body = client.post("/decisions/d1/approve").json()
    assert body["ok"] is False and body["entry_sent"] is True and body["kind"] == KIND_ENTRY_UNKNOWN
    assert resolution(state.journal, "d1") == "approved" and done == ["d1"]


def test_executor_crash_is_unknown_outcome_and_not_rolled_back(tmp_path):
    def boom(d, m):
        raise RuntimeError("journal write failed after entry")

    client, state, done = make_api(tmp_path, boom)
    add_pending(state.journal, "d1")
    body = client.post("/decisions/d1/approve").json()
    assert body["ok"] is False and body["kind"] == "error" and "исход неизвестен" in body["note"]
    assert resolution(state.journal, "d1") == "approved"  # мог упасть после входа — без повтора


def test_no_executor_409_and_nothing_recorded(tmp_path):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    client = TestClient(build_app(state))
    add_pending(state.journal, "d1")
    r = client.post("/decisions/d1/approve")
    assert r.status_code == 409 and "не подключено" in r.json()["detail"]
    assert resolution(state.journal, "d1") is None


def test_entry_param_and_validation(tmp_path):
    calls = []
    client, state, _ = make_api(
        tmp_path, lambda d, m: calls.append(m) or ExecutionOutcome(True, f"{m} ok", entry_type=m)
    )
    add_pending(state.journal, "d1")
    add_pending(state.journal, "d2")
    r = client.post("/decisions/d1/approve", params={"entry": "yolo"})
    assert r.status_code == 422 and resolution(state.journal, "d1") is None and calls == []
    assert client.post("/decisions/d2/approve", params={"entry": "limit"}).json()["entry_type"] == "limit"
    assert calls == ["limit"]


def test_pause_and_unknown_do_not_execute(tmp_path):
    calls = []
    client, state, _ = make_api(tmp_path, lambda d, m: calls.append(d) or ExecutionOutcome(True, "x"))
    add_pending(state.journal, "d1")
    client.post("/pause")
    assert client.post("/decisions/d1/approve").status_code == 423
    assert client.post("/decisions/nope/approve").status_code == 404
    assert calls == []
    assert client.post("/decisions/d1/reject").json()["resolution"] == "rejected"  # reject как был


# ------------------------------------------------------- тип входа по умолчанию


def _decision(j, decision_id, chosen: str | None):
    cand = {"entry_options": [{"id": "e-m", "kind": "market", "price": 100.0},
                              {"id": "e-l", "kind": "limit_fvg_edge", "price": 101.8}]}
    trade = json.dumps({"verdict": "execute", "entry_option_id": chosen}) if chosen else None
    j.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, trade_json, outcome) "
        "VALUES ('r1', ?, ?, ?, 'pending')", (decision_id, json.dumps(cand), trade),
    )
    j.commit()


def _card(j, decision_id, **plan_kw):
    add_pending(j, decision_id)
    j.execute("UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
              (plan(candidate_id=decision_id, **plan_kw).model_dump_json(), decision_id))
    j.commit()


def test_default_entry_type_follows_trader_choice(tmp_path):
    j = connect(tmp_path / "j.db")
    _card(j, "m1")
    _decision(j, "m1", "e-m")  # Trader: маркет (лимитный уровень в плане все равно есть)
    _card(j, "l1")
    _decision(j, "l1", "e-l")  # Trader: лимитка
    _card(j, "n1", entry_limit=None)
    _decision(j, "n1", "e-l")  # уровня в плане нет — только маркет
    _card(j, "u1")  # выбора Trader'а в журнале нет — по плану: уровень есть -> limit
    assert default_entry_type(j, "m1") == "market"
    assert default_entry_type(j, "l1") == "limit"
    assert default_entry_type(j, "n1") == "market"
    assert default_entry_type(j, "u1") == "limit"
    assert default_entry_type(j, "missing") == "market"

    seen = []
    res = approve_and_execute(j, "m1", lambda d, m: seen.append(m) or ExecutionOutcome(True, "ok"))
    assert res.ok and seen == ["market"]


# ------------------------------------------------------------ позиции и P&L


def test_positions_endpoint_lists_open_and_pending(tmp_path):
    client, state, _ = make_api(tmp_path, None)
    j = state.journal
    add_trade(j, "o1", sl=98.0, tp=104.0)
    add_trade(j, "p1", status="pending", symbol="B/USDT:USDT", side="short", entry=10.0, sl=10.5, tp=9.0)
    add_trade(j, "c1", status="closed", symbol="C/USDT:USDT")
    body = client.get("/positions").json()
    assert body["open"] == 1 and body["pending"] == 1
    rows = {r["decision_id"]: r for r in body["positions"]}
    assert set(rows) == {"o1", "p1"}
    assert rows["o1"]["symbol"] == "SOL/USDT:USDT" and rows["o1"]["side"] == "long"
    o1 = rows["o1"]
    assert (o1["entry"], o1["sl"], o1["tp"], o1["qty"]) == (100.0, 98.0, 104.0, 2.0)
    assert rows["p1"]["status"] == "pending" and rows["p1"]["sl"] == 10.5


def test_pnl_endpoint_splits_reconciled(tmp_path):
    from datetime import UTC, datetime, timedelta

    from oskar.core.position_monitor import _iso

    client, state, _ = make_api(tmp_path, None)
    j = state.journal
    now = datetime.now(UTC)
    for did, pnl, rec, age_h in (("a", 5.0, 1, 1), ("b", -2.0, 0, 2), ("c", None, 0, 3), ("old", 9.0, 1, 50)):
        add_trade(j, did, status="closed", symbol=f"{did}/USDT:USDT")
        j.execute("UPDATE trades SET realized_pnl=?, reconciled=?, closed_at=? WHERE decision_id=?",
                  (pnl, rec, _iso(now - timedelta(hours=age_h)), did))
    j.commit()
    day = client.get("/pnl").json()
    assert day["closed"] == 3 and day["pnl_reconciled"] == 5.0 and day["n_reconciled"] == 1
    assert day["pnl_unreconciled"] == -2.0 and day["n_unreconciled"] == 1 and day["n_no_pnl"] == 1
    assert day["wins"] == 1 and day["losses"] == 1
    everything = client.get("/pnl", params={"hours": 0}).json()
    assert everything["closed"] == 4 and everything["pnl_reconciled"] == 14.0 and everything["hours"] is None


# --------------------------------------------------------------- скрипты


def test_oskarctl_new_commands_map_to_api():
    ctl = _load_script("skills/oskar-control/scripts/oskarctl.py")
    assert ctl.request_for(["positions"]) == ("GET", "/positions")
    assert ctl.request_for(["pnl"]) == ("GET", "/pnl")
    assert ctl.request_for(["pnl", "168"]) == ("GET", "/pnl?hours=168")
    assert ctl.request_for(["mode"]) == ("GET", "/mode")
    assert ctl.request_for(["mode", "auto"]) == ("POST", "/mode/auto")
    assert ctl.request_for(["mode", "confirm"]) == ("POST", "/mode/confirm")
    assert ctl.request_for(["status"]) == ("GET", "/status")
    for bad in (["mode", "yolo"], ["pnl", "x"], ["pnl", "-1"], ["status", "extra"], ["nope"], []):
        assert ctl.request_for(bad) is None, bad


def test_skill_docs_match_scripts():
    """SKILL.md обещает только то, что умеет скрипт (раньше — /positions и /pnl без команд)."""
    ctl = _load_script("skills/oskar-control/scripts/oskarctl.py")
    doc = (REPO / "skills/oskar-control/SKILL.md").read_text(encoding="utf-8")
    for command in ctl.COMMANDS:
        assert f"oskarctl.py {command}" in doc, command
    approvals = (REPO / "skills/oskar-approvals/SKILL.md").read_text(encoding="utf-8")
    assert "Исполняю" not in approvals.replace("«Исполняю…»", "")


def test_approve_script_reports_actual_outcome():
    ap = _load_script("skills/oskar-approvals/scripts/approve.py")
    assert ap.summarize({"ok": True, "note": "маркет: SOL long"}).startswith("ИСПОЛНЕНО")
    refused = ap.summarize({"ok": False, "note": "цена ушла", "kind": "refused_precheck",
                            "entry_sent": False, "resolution": None})
    assert refused.startswith("НЕ ИСПОЛНЕНО") and "карточка жива" in refused
    sent = ap.summarize({"ok": False, "note": "стоп не встал", "entry_sent": True,
                         "resolution": "approved", "kind": "protection_failed"})
    assert sent.startswith("ВХОД УШЕЛ") and "НЕ подтверждать" in sent
    unknown = ap.summarize({"ok": False, "note": "x", "entry_sent": False, "resolution": "approved"})
    assert unknown.startswith("ИСХОД НЕИЗВЕСТЕН")
