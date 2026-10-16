"""Режим исполнения «автомат / с подтверждением» (Иван 2026-10-16): ползунок в боте,
автомат только на paper/VST, предохранитель по отказам биржи."""

import httpx
import pytest

from oskar.core.escalation import Escalator
from oskar.core.executor import ExecutionOutcome
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller, mode_message
from oskar.core.trading_mode import AUTO, BREAKER_FAILURES, CONFIRM, AutoExecutor, TradingMode
from oskar.journal.db import connect
from tests.unit.test_buttons import CHAT, FakeBotApi, add_pending, callback_update
from tests.unit.test_escalation import CAPS, PROFILE, candidate, resolve_fn, sv, td

# --------------------------------------------------------------------- режим


def test_mode_default_persist_and_paper_only(tmp_path):
    j = connect(tmp_path / "j.db")
    m = TradingMode(j, allowed=True, default=AUTO)
    assert m.get() == AUTO and m.is_auto()
    m.set(CONFIRM)
    assert TradingMode(j, allowed=True, default=AUTO).get() == CONFIRM  # переживает рестарт
    live = TradingMode(j, allowed=False, default=AUTO)
    assert live.get() == CONFIRM  # вне paper/VST автомата нет при любых настройках
    with pytest.raises(ValueError):
        live.set(AUTO)
    with pytest.raises(ValueError):
        m.set("yolo")


# ----------------------------------------------------------------- автомат


def _auto(tmp_path, execute, notify=None):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    mode = TradingMode(state.journal, allowed=True, default=AUTO)
    return AutoExecutor(mode, state, execute, notify=notify), state, mode


def test_auto_success_approves_and_executes(tmp_path):
    calls = []
    auto, state, _ = _auto(
        tmp_path, lambda d, m: calls.append((d, m)) or ExecutionOutcome(True, "маркет: SOL")
    )
    add_pending(state.journal, "d1")
    res = auto.run("d1", "market")
    assert res.executed and "автомат" in res.note and calls == [("d1", "market")]
    row = state.journal.execute("SELECT resolution FROM pending_approvals WHERE decision_id='d1'").fetchone()
    assert row["resolution"] == "approved"


def test_auto_refused_rolls_back_approval_for_manual_decision(tmp_path):
    auto, state, _ = _auto(tmp_path, lambda d, m: ExecutionOutcome(False, "уже есть позиция SOL long"))
    add_pending(state.journal, "d1")
    res = auto.run("d1", "limit")
    assert not res.executed and "не исполнил" in res.note
    row = state.journal.execute("SELECT resolution FROM pending_approvals WHERE decision_id='d1'").fetchone()
    assert row["resolution"] is None  # карточка уйдет с кнопками


def test_auto_blocked_in_safe_mode(tmp_path):
    calls = []
    auto, state, _ = _auto(tmp_path, lambda d, m: calls.append(d) or ExecutionOutcome(True, "x"))
    add_pending(state.journal, "d1")
    state.safe_mode = True
    res = auto.run("d1", "market")
    assert not res.executed and calls == []


def test_breaker_switches_back_to_confirm(tmp_path):
    notes = []
    auto, state, mode = _auto(
        tmp_path, lambda d, m: ExecutionOutcome(False, "биржа отказала: [80001] bad"), notify=notes.append
    )
    for i in range(BREAKER_FAILURES):
        add_pending(state.journal, f"d{i}")
        auto.run(f"d{i}", "market")
    assert mode.get() == CONFIRM and any("Автомат выключен" in n for n in notes)


def test_entry_sent_counts_as_executed(tmp_path):
    """Вход ушел, защита не встала — сделка в журнале: кнопки не нужны, approve не откатываем."""
    auto, state, _ = _auto(
        tmp_path, lambda d, m: ExecutionOutcome(False, "вход исполнен, стоп не встал", entry_sent=True)
    )
    add_pending(state.journal, "d1")
    assert auto.run("d1", "market").executed


# ----------------------------------------------------------- эскалатор в автомате


def _escalator(tmp_path, cards, execute, mode_value=AUTO):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    mode = TradingMode(state.journal, allowed=True, default=mode_value)
    auto = AutoExecutor(mode, state, execute)

    def sink(c, v, s, p=None, note=None, buttons=True):
        cards.append({"id": c.id, "note": note, "buttons": buttons})
        return 101

    esc = Escalator(
        state,
        CAPS,
        PROFILE,
        scout_fn=lambda c: sv(),
        trader_fn=lambda c, v: td(),
        resolve_fn=resolve_fn,
        approval_ttl_seconds=1200,
        card_sink=sink,
        equity_fn=lambda: 1000.0,
        auto_executor=auto,
    )
    return esc, state


def test_escalator_auto_executes_and_sends_notice_without_buttons(tmp_path):
    cards, calls = [], []
    esc, state = _escalator(
        tmp_path, cards, lambda d, m: calls.append(m) or ExecutionOutcome(True, "маркет: SOL")
    )
    res = esc(candidate())
    assert res.outcome == "executed" and calls == ["market"]
    assert cards == [{"id": "SOLUSDTUSDT-100-short", "note": "🤖 автомат: маркет: SOL", "buttons": False}]


def test_escalator_auto_failure_falls_back_to_buttons(tmp_path):
    cards = []
    esc, state = _escalator(tmp_path, cards, lambda d, m: ExecutionOutcome(False, "цена ушла на 0.9%"))
    res = esc(candidate())
    assert res.outcome == "pending" and cards[0]["buttons"] is True and "не исполнил" in cards[0]["note"]


def test_escalator_confirm_mode_unchanged(tmp_path):
    cards, calls = [], []
    esc, state = _escalator(tmp_path, cards, lambda d, m: calls.append(d), mode_value=CONFIRM)
    res = esc(candidate())
    assert (
        res.outcome == "pending" and calls == [] and cards[0]["buttons"] is True and cards[0]["note"] is None
    )


# ------------------------------------------------------------- ползунок в боте


def _poller(tmp_path, updates, on_mode):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    fake = FakeBotApi(updates)
    poller = ButtonPoller("TOKEN", CHAT, state, client=httpx.Client(transport=fake), on_mode=on_mode)
    return poller, fake


def _message(text, user=None):
    return {
        "update_id": 200,
        "message": {
            "message_id": 5,
            "date": 0,
            "text": text,
            "chat": {"id": int(CHAT)},
            "from": {"id": int(user or CHAT)},
        },
    }


def test_mode_command_replies_with_slider(tmp_path):
    poller, fake = _poller(tmp_path, [_message("/mode")], on_mode=lambda t: AUTO)
    poller.poll_once(timeout=0)
    sent = next(b for p, b in fake.calls if p == "sendMessage")
    keys = [b["callback_data"] for b in sent["reply_markup"]["inline_keyboard"][0]]
    assert keys == ["oskar:mode:auto", "oskar:mode:confirm"] and "автомат" in sent["text"]


def test_mode_button_switches_and_redraws(tmp_path):
    switched = []
    poller, fake = _poller(
        tmp_path, [callback_update("oskar:mode:confirm")], on_mode=lambda t: switched.append(t) or (t or AUTO)
    )
    poller.poll_once(timeout=0)
    assert switched == ["confirm"]
    answer = next(b for p, b in fake.calls if p == "answerCallbackQuery")
    assert "с подтверждением" in answer["text"]
    assert any(p == "editMessageText" for p, _ in fake.calls)


def test_mode_button_from_stranger_ignored(tmp_path):
    switched = []
    poller, fake = _poller(
        tmp_path,
        [callback_update("oskar:mode:auto", user="999")],
        on_mode=lambda t: switched.append(t) or AUTO,
    )
    poller.poll_once(timeout=0)
    assert switched == [] and all(p != "answerCallbackQuery" for p, _ in fake.calls)


def test_mode_message_marks_active():
    text, kb = mode_message(CONFIRM)
    assert kb["inline_keyboard"][0][1]["text"].startswith("●") and "подтверждаешь" in text


# --------------------------------------------------------------------- API


def test_api_mode_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    mode = TradingMode(state.journal, allowed=True, default=CONFIRM)
    client = TestClient(build_app(state, trading_mode=mode))
    assert client.get("/mode").json() == {"mode": "confirm", "auto_allowed": True}
    assert client.post("/mode/auto").json() == {"mode": "auto"}
    assert client.post("/mode/nope").status_code == 409
