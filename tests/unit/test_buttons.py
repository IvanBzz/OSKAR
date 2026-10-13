"""Кнопки: резолвер подтверждений + поллер сигнального бота (фейковый Bot API)."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from oskar.core.approvals import resolve_approval
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller
from oskar.journal.db import connect

CHAT = "569880715"


def add_pending(journal, decision_id="d1", expires_in_min=10):
    expires = (datetime.now(UTC) + timedelta(minutes=expires_in_min)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    journal.execute(
        "INSERT INTO pending_approvals (decision_id, card_json, expires_at) VALUES (?,?,?)",
        (decision_id, "{}", expires),
    )
    journal.commit()


# ----------------------------------------------------------------- approvals


def test_snooze_extends_ttl(tmp_path):
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1", expires_in_min=2)
    before = j.execute("SELECT expires_at FROM pending_approvals").fetchone()[0]
    out = resolve_approval(j, "d1", "snooze")
    assert out.ok and out.resolution == "snoozed"
    after = j.execute("SELECT expires_at FROM pending_approvals").fetchone()[0]
    assert after > before
    # карточка все еще живая — approve работает
    assert resolve_approval(j, "d1", "approve").ok
    j.close()


def test_approve_blocked_in_safe_mode(tmp_path):
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1")
    out = resolve_approval(j, "d1", "approve", safe_mode=True)
    assert not out.ok and out.code == 423
    assert resolve_approval(j, "d1", "reject", safe_mode=True).ok  # reject всегда
    j.close()


# -------------------------------------------------------------------- poller


class FakeBotApi(httpx.BaseTransport):
    def __init__(self, updates):
        self.updates = updates
        self.calls: list[tuple[str, dict]] = []
        self.served = False

    def handle_request(self, request):
        request.read()
        path = request.url.path.split("/")[-1]
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        self.calls.append((path, body or dict(request.url.params)))
        if path == "getUpdates":
            if self.served:
                return httpx.Response(200, json={"ok": True, "result": []})
            self.served = True
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(200, json={"ok": True, "result": {}})


def callback_update(data, chat=CHAT, update_id=100):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"message_id": 7, "chat": {"id": int(chat)}, "caption": "карточка"},
        },
    }


def make_poller(tmp_path, updates, on_resolution=None):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    fake = FakeBotApi(updates)
    poller = ButtonPoller(
        "TOKEN", CHAT, state,
        client=httpx.Client(transport=fake), on_resolution=on_resolution,
    )
    return poller, state, fake


def test_market_button_resolves_and_executes(tmp_path):
    """Кнопка «Маркет» = approve + исполнение одним нажатием."""
    calls = []
    poller, state, fake = make_poller(
        tmp_path, [callback_update("oskar:market:d1")],
        on_resolution=lambda d, mode: calls.append((d, mode)),
    )
    add_pending(state.journal, "d1")
    assert poller.poll_once(timeout=0) == 1

    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='d1'"
    ).fetchone()
    assert row["resolution"] == "approved"
    assert calls == [("d1", "market")]  # исполнителю передан способ входа

    paths = [p for p, _ in fake.calls]
    assert "answerCallbackQuery" in paths
    assert "editMessageCaption" in paths  # кнопки убраны, статус дописан


def test_plain_approve_does_not_execute(tmp_path):
    """Голый approve (через API/Hermes) решение фиксирует, но ордер не ставит —
    способ входа выбирается только кнопкой Маркет/Лимит."""
    calls = []
    poller, state, fake = make_poller(
        tmp_path, [callback_update("oskar:approve:d1")],
        on_resolution=lambda d, mode: calls.append((d, mode)),
    )
    add_pending(state.journal, "d1")
    poller.poll_once(timeout=0)
    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='d1'"
    ).fetchone()
    assert row["resolution"] == "approved" and calls == []


def test_foreign_chat_ignored_silently(tmp_path):
    poller, state, fake = make_poller(
        tmp_path, [callback_update("oskar:approve:d1", chat="999")]
    )
    add_pending(state.journal, "d1")
    poller.poll_once(timeout=0)
    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='d1'"
    ).fetchone()
    assert row["resolution"] is None  # не тронуто
    assert all(p != "answerCallbackQuery" for p, _ in fake.calls)  # чужим не отвечаем


def test_expired_button_answers_failclosed(tmp_path):
    poller, state, fake = make_poller(tmp_path, [callback_update("oskar:approve:old")])
    add_pending(state.journal, "old", expires_in_min=-5)
    poller.poll_once(timeout=0)
    answer = next(b for p, b in fake.calls if p == "answerCallbackQuery")
    assert "TTL" in answer["text"]
    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='old'"
    ).fetchone()
    assert row["resolution"] == "expired"


def test_ping_button_answers_without_touching_journal(tmp_path):
    """Самопроверка цепочки: ответ всплывашкой + снятие кнопок, журнал не тронут."""
    from oskar.core.telegram_buttons import PING_ANSWER, send_ping_card

    poller, state, fake = make_poller(tmp_path, [callback_update("oskar:ping:selftest")])
    assert poller.poll_once(timeout=0) == 1
    answer = next(b for p, b in fake.calls if p == "answerCallbackQuery")
    assert answer["text"] == PING_ANSWER
    assert "editMessageCaption" in [p for p, _ in fake.calls]
    assert state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0] == 0

    # сама карточка: одна кнопка с ping-callback, уходит через sendMessage
    assert send_ping_card("TOKEN", CHAT, client=httpx.Client(transport=fake))
    sent = next(b for p, b in fake.calls if p == "sendMessage")
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "oskar:ping:selftest"


def test_not_ok_response_signals_backoff(tmp_path):
    """401 (отозванный токен) и т.п.: poll_once -> -1, стрик растет, лог не спамит.
    Регресс 2026-10-12: tight-loop, 3 запроса за 150 мс."""

    class RevokedApi(FakeBotApi):
        def handle_request(self, request):
            request.read()
            self.calls.append((request.url.path.split("/")[-1], {}))
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    fake = RevokedApi([])
    poller = ButtonPoller("TOKEN", CHAT, state, client=httpx.Client(transport=fake))
    assert poller.poll_once(timeout=0) == -1
    assert poller.poll_once(timeout=0) == -1
    assert poller.not_ok_streak == 2


def test_offset_advances(tmp_path):
    poller, state, fake = make_poller(tmp_path, [callback_update("oskar:reject:d1", update_id=555)])
    add_pending(state.journal, "d1")
    poller.poll_once(timeout=0)
    poller.poll_once(timeout=0)
    last_get = [b for p, b in fake.calls if p == "getUpdates"][-1]
    assert int(last_get["offset"]) == 556  # следующий запрос с offset+1
