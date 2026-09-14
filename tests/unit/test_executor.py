"""Исполнение по кнопке: маркет/лимит, идемпотентность, отказ биржи, кнопки."""

import json

import httpx
import pytest

from oskar.contracts.schemas import ExecutionPlan
from oskar.core.executor import ExecutionService
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller
from oskar.core.telegram_out import build_keyboard
from oskar.execution.paper import PaperAdapter
from oskar.journal.db import connect
from tests.unit.test_buttons import CHAT, FakeBotApi, add_pending, callback_update


def plan(**overrides) -> ExecutionPlan:
    base = dict(
        candidate_id="SOLUSDTUSDT-299-short",
        symbol="SOL/USDT:USDT",
        side="short",
        entry_market=100.0,
        entry_limit=101.8,
        entry_limit_note="FVG 101-102",
        sl=103.5,
        tp=95.0,
        qty=2.7429,
        leverage=5.4,
        risk_usd=1.22,
        rr_fee_adjusted=2.42,
        timeframe="15m",
        tldr="Свип хая и ретест FVG; цель — ликвидность снизу.",
    )
    base.update(overrides)
    return ExecutionPlan(**base)


def make_service(tmp_path, plan_obj=None, price=100.0):
    j = connect(tmp_path / "j.db")
    p = plan_obj or plan()
    add_pending(j, p.candidate_id)
    j.execute(
        "UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
        (p.model_dump_json(), p.candidate_id),
    )
    j.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
        "VALUES ('r1', ?, '{}', 'pending')", (p.candidate_id,),
    )
    j.commit()
    adapter = PaperAdapter(lambda s: price, start_balance=121.71)
    return ExecutionService(adapter, j, "r1"), j, adapter, p


def test_market_execution_places_entry_sl_tp(tmp_path):
    svc, j, adapter, p = make_service(tmp_path)
    out = svc.execute(p.candidate_id, "market")
    assert out.ok, out.message
    assert "маркет" in out.message

    row = j.execute("SELECT * FROM trades WHERE decision_id=?", (p.candidate_id,)).fetchone()
    assert row["status"] == "open"
    assert row["symbol"] == "SOL/USDT:USDT" and row["side"] == "short"
    assert row["sl_price"] == 103.5 and row["tp_price"] == 95.0
    assert row["leverage"] == 5.4
    assert row["client_order_id"].startswith("OSKAR-r1-")

    positions = adapter.positions()
    assert len(positions) == 1 and positions[0].qty == pytest.approx(2.7429)

    dec = j.execute(
        "SELECT outcome, outcome_detail FROM decisions WHERE decision_id=?", (p.candidate_id,)
    ).fetchone()
    assert dec["outcome"] == "executed" and "market@100" in dec["outcome_detail"]
    j.close()


def test_limit_execution_uses_found_level(tmp_path):
    """Кнопка «Лимит» ставит отложенный ордер по найденному уровню (не по рынку)."""
    svc, j, adapter, p = make_service(tmp_path)
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok and "лимит 101.8" in out.message

    row = j.execute("SELECT * FROM trades WHERE decision_id=?", (p.candidate_id,)).fetchone()
    assert row["entry_price"] == 101.8  # уровень FVG, а не рыночная цена
    assert row["status"] == "pending"   # ждет исполнения
    j.close()


def test_double_click_is_noop(tmp_path):
    """Дубль нажатия не создает вторую позицию (класс бага v1)."""
    svc, j, adapter, p = make_service(tmp_path)
    assert svc.execute(p.candidate_id, "market").ok
    second = svc.execute(p.candidate_id, "market")
    assert not second.ok and "уже исполнено" in second.message
    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert len(adapter.positions()) == 1
    j.close()


def test_limit_unavailable_without_level(tmp_path):
    svc, j, adapter, p = make_service(tmp_path, plan_obj=plan(entry_limit=None))
    out = svc.execute(p.candidate_id, "limit")
    assert not out.ok and "только маркет" in out.message
    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    j.close()


def test_exchange_failure_writes_no_trade(tmp_path):
    """Биржа отказала — сделки в журнале нет (журнал не врет, ст. 1)."""
    svc, j, adapter, p = make_service(tmp_path)

    def boom(*a, **kw):
        raise ConnectionError("bingx 109400")

    adapter.place_entry = boom
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "биржа отказала" in out.message
    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    j.close()


# --------------------------------------------------------------------- кнопки


def test_keyboard_has_market_and_limit():
    rows = build_keyboard("SOLUSDTUSDT-299-short", plan())
    flat = [b for row in rows for b in row]
    labels = [b["text"] for b in flat]
    assert any("Маркет" in x for x in labels)
    assert any("Лимит 101.8" in x for x in labels)
    for b in flat:
        assert len(b["callback_data"].encode()) <= 64  # лимит Telegram


def test_keyboard_without_limit_level():
    rows = build_keyboard("d1", plan(entry_limit=None))
    labels = [b["text"] for row in rows for b in row]
    assert not any("Лимит" in x for x in labels)


def test_button_market_triggers_execution(tmp_path):
    """Сквозной путь: нажатие -> approve -> ордера -> ответ с деталями."""
    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    p = plan()
    add_pending(j, p.candidate_id)
    j.execute(
        "UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
        (p.model_dump_json(), p.candidate_id),
    )
    j.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
        "VALUES ('r1', ?, '{}', 'pending')", (p.candidate_id,),
    )
    j.commit()
    svc = ExecutionService(PaperAdapter(lambda s: 100.0), j, "r1")

    fake = FakeBotApi([callback_update(f"oskar:market:{p.candidate_id}")])
    poller = ButtonPoller(
        "TOKEN", CHAT, state, client=httpx.Client(transport=fake),
        on_resolution=svc.execute,
    )
    poller.poll_once(timeout=0)

    assert j.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    answer = next(b for path, b in fake.calls if path == "answerCallbackQuery")
    assert "SOL/USDT:USDT" in answer["text"] and "плечо" in answer["text"]
    j.close()


def test_button_execution_failure_keeps_card_open(tmp_path):
    """Отказ биржи — решение откатывается, карточка остается живой."""
    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    p = plan()
    add_pending(j, p.candidate_id)
    j.execute(
        "UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
        (p.model_dump_json(), p.candidate_id),
    )
    j.commit()

    def failing(decision_id, mode):
        from oskar.core.executor import ExecutionOutcome

        return ExecutionOutcome(False, "биржа отказала: timeout")

    fake = FakeBotApi([callback_update(f"oskar:market:{p.candidate_id}")])
    poller = ButtonPoller(
        "TOKEN", CHAT, state, client=httpx.Client(transport=fake), on_resolution=failing,
    )
    poller.poll_once(timeout=0)

    row = j.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id=?", (p.candidate_id,)
    ).fetchone()
    assert row["resolution"] is None  # откат: можно нажать снова
    answer = next(b for path, b in fake.calls if path == "answerCallbackQuery")
    assert "не исполнено" in answer["text"]
    j.close()


def test_plan_json_roundtrip():
    p = plan()
    assert ExecutionPlan.model_validate_json(p.model_dump_json()) == p
    assert json.loads(p.model_dump_json())["schema_version"] == 1
