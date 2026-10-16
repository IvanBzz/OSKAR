"""Ст. 6 Emergency Stop: одна функция на три поверхности (ревью 2026-10-13).

API, Telegram /panic сигнального бота и CLI `oskar panic` сходятся в
core/emergency.engage_panic: флаги -> meta -> биржа. Telegram — фейковый Bot API,
биржа — FakeAdapter монитора, CLI — stdlib HTTP-сервер на эфемерном порту
(живой oskard на 8321 тесты не трогают никогда).
"""

import http.server
import json
import socket
import sqlite3
import sys
import threading
import time

import httpx
import pytest

from oskar import cli
from oskar.core.emergency import engage_panic, panic_engaged, reset_panic
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller, parse_command, status_line
from oskar.execution.base import OrderState, PositionInfo
from oskar.journal.db import connect
from tests.unit.test_buttons import CHAT, FakeBotApi, add_pending, callback_update
from tests.unit.test_position_monitor import add_trade, make_monitor, trade


class LockedJournal:
    """sqlite, у которого любая запись падает (диск/замок) — commit/rollback живы."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        pass

    def rollback(self):
        pass


# ------------------------------------------------------------------- core


def test_engage_panic_sets_flags_before_persist_and_exchange(tmp_path):
    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    seen = {}

    class Monitor:
        def panic_flatten(self):
            if seen:  # финальный проход (всегда, ревью Codex H2/I): первый уже все снял
                return {"cancelled": 0, "closed": 0, "errors": []}
            seen["flags"] = (state.safe_mode, state.paused, state.panic_at is not None)
            seen["persisted"] = panic_engaged(j) is not None
            return {"cancelled": 2, "closed": 1, "errors": []}

    out = engage_panic(state, Monitor())
    assert seen == {"flags": (True, True, True), "persisted": True}  # биржа — последней
    assert out == {"panic": True, "safe_mode": True, "paused": True,
                   "exchange": {"cancelled": 2, "closed": 1, "errors": []}}


def test_engage_panic_survives_journal_and_exchange_failures():
    state = RuntimeState(journal=LockedJournal())

    class DeadExchange:
        def panic_flatten(self):
            raise RuntimeError("bingx timeout")

    out = engage_panic(state, DeadExchange())
    assert state.safe_mode and state.paused and state.panic_at is not None
    assert "locked" in out["persist_error"]  # оператор видит, что рестарт panic не удержит
    assert out["exchange"] == {"error": "bingx timeout"}


def test_reset_panic_keeps_panic_when_journal_write_fails(tmp_path):
    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    engage_panic(state)
    state.journal = LockedJournal()
    with pytest.raises(sqlite3.OperationalError):
        reset_panic(state)
    assert state.panic_at is not None and state.safe_mode  # не записалось -> panic взведен целиком

    state.journal = j
    assert reset_panic(state) == {"panic": False, "safe_mode": False, "paused": True}
    assert panic_engaged(j) is None and state.paused  # пауза снимается отдельным resume


# --------------------------------------------------------------- telegram


def message_update(text, chat=CHAT, user=None, update_id=200, date=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 9,
            "date": int(time.time()) if date is None else date,
            "text": text,
            "chat": {"id": int(chat), "type": "private"},
            "from": {"id": int(user if user is not None else chat), "is_bot": False},
        },
    }


def make_poller(tmp_path, updates, **kwargs):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    fake = FakeBotApi(updates)
    poller = ButtonPoller("TOKEN", CHAT, state, client=httpx.Client(transport=fake), **kwargs)
    return poller, state, fake


def replies(fake) -> list[str]:
    return [body["text"] for path, body in fake.calls if path == "sendMessage"]


def test_poller_asks_telegram_for_messages(tmp_path):
    poller, state, fake = make_poller(tmp_path, [])
    poller.poll_once(timeout=0)
    params = next(body for path, body in fake.calls if path == "getUpdates")
    assert set(json.loads(params["allowed_updates"])) == {"callback_query", "message"}


def test_telegram_panic_reaches_exchange_and_replies(tmp_path):
    """Связка как в daemon.main: on_panic = engage_panic(state, monitor)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "pend", status="pending", symbol="A/USDT:USDT")
    add_trade(j, "open1", symbol="B/USDT:USDT")
    ex.by_cid["OSKAR-r1-pend"] = OrderState("e-pend", "open")
    ex.orders["e-pend"] = OrderState("e-pend", "canceled")  # после отмены: panic перепроверяет по id
    ex.pos = [PositionInfo("B/USDT:USDT", "long", 2.0, 100.0, -1.0)]
    fake = FakeBotApi([message_update("/panic")])
    poller = ButtonPoller(
        "TOKEN", CHAT, state, client=httpx.Client(transport=fake),
        on_panic=lambda: engage_panic(state, mon),
    )
    assert poller.poll_once(timeout=0) == 1
    assert state.safe_mode and state.paused and panic_engaged(j) is not None
    assert "e-pend" in ex.cancelled and ex.closed == [("B/USDT:USDT", "long")]
    assert trade(j, "open1")["close_reason"] == "panic"
    reply = replies(fake)[-1]
    assert "PANIC" in reply and "Снято входов: 1" in reply and "закрыто позиций: 1" in reply
    assert "panic-reset" in reply


def test_telegram_panic_from_strangers_is_ignored_silently(tmp_path):
    calls = []
    poller, state, fake = make_poller(
        tmp_path,
        [message_update("/panic", chat="999", update_id=1),  # чужой чат
         message_update("/panic", user="777", update_id=2)],  # чужой автор в нашем чате
        on_panic=lambda: calls.append(1),
    )
    poller.poll_once(timeout=0)
    assert calls == [] and not state.safe_mode and state.panic_at is None
    assert replies(fake) == []  # чужим не отвечаем


def test_telegram_panic_without_monitor_still_engages(tmp_path):
    poller, state, fake = make_poller(tmp_path, [message_update("/panic@OskarSignalsBot")])
    poller.poll_once(timeout=0)
    assert state.safe_mode and panic_engaged(state.journal) is not None  # флаги + meta по умолчанию
    assert "НЕ тронута" in replies(fake)[-1]


def test_telegram_panic_runs_before_buttons_of_same_batch(tmp_path):
    """Нажатие «Маркет» и /panic в одной пачке (демон лежал): ордер не ставится."""
    executed = []
    poller, state, fake = make_poller(
        tmp_path,
        [callback_update("oskar:market:d1", update_id=100), message_update("/panic", update_id=101)],
        on_resolution=lambda d, m: executed.append(d),
    )
    add_pending(state.journal, "d1")
    assert poller.poll_once(timeout=0) == 2
    assert executed == []
    row = state.journal.execute("SELECT resolution FROM pending_approvals WHERE decision_id='d1'").fetchone()
    assert row["resolution"] is None  # approve отвергнут safe_mode'ом
    poller.poll_once(timeout=0)
    last_get = [body for path, body in fake.calls if path == "getUpdates"][-1]
    assert int(last_get["offset"]) == 102  # offset — по максимуму пачки, не по порядку обработки


def test_telegram_panic_reply_escapes_dynamic_text(tmp_path):
    hour_ago = int(time.time()) - 3600
    poller, state, fake = make_poller(
        tmp_path, [message_update("/panic", date=hour_ago)],
        on_panic=lambda: {"exchange": {"cancelled": 0, "closed": 0, "errors": ["<b>d1</b> & co"]},
                          "persist_error": "disk <full>"},
    )
    poller.poll_once(timeout=0)
    reply = replies(fake)[-1]
    assert "&lt;b&gt;d1&lt;/b&gt; &amp; co" in reply and "<b>d1</b>" not in reply
    assert "disk &lt;full&gt;" in reply
    assert "ждала 60 мин" in reply  # команда пролежала в очереди, пока демон лежал


def test_telegram_panic_handler_failure_keeps_flags(tmp_path):
    def broken():
        raise RuntimeError("monitor <gone>")

    poller, state, fake = make_poller(tmp_path, [message_update("/panic")], on_panic=broken)
    poller.poll_once(timeout=0)
    assert state.safe_mode and state.paused and state.panic_at is not None
    reply = replies(fake)[-1]
    assert "упало" in reply and "monitor &lt;gone&gt;" in reply


def test_telegram_status_answers_and_other_text_is_silent(tmp_path):
    asked = []
    poller, state, fake = make_poller(
        tmp_path,
        [message_update("/status", update_id=1), message_update("привет", update_id=2),
         message_update("/start", update_id=3)],
        on_status=lambda: asked.append(1) or "OSKAR <r1>: работает",
    )
    assert poller.poll_once(timeout=0) == 3
    assert asked == [1] and replies(fake) == ["OSKAR &lt;r1&gt;: работает"]
    assert not state.safe_mode


def test_message_offset_advances(tmp_path):
    poller, state, fake = make_poller(tmp_path, [message_update("hello", update_id=700)])
    poller.poll_once(timeout=0)
    poller.poll_once(timeout=0)
    last_get = [body for path, body in fake.calls if path == "getUpdates"][-1]
    assert int(last_get["offset"]) == 701


def test_parse_command():
    assert parse_command("/panic") == "panic"
    assert parse_command(" /PANIC@OskarSignalsBot сейчас") == "panic"
    assert parse_command("panic") is None and parse_command("/") is None and parse_command("") is None


def test_status_line_summarizes_state(tmp_path):
    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j, run_id="r1")
    add_trade(j, "o1")
    add_trade(j, "p1", status="pending", symbol="B/USDT:USDT")
    add_pending(j, "card1")

    class Runner:
        equity_usd, equity_source = 121.714, "exchange"

    line = status_line(state, Runner())
    assert line.startswith("OSKAR r1: работает")
    assert "позиций 1, лимиток 1, карточек 1" in line and "equity $121.71 (exchange)" in line
    state.panic()
    assert "PANIC с" in status_line(state)


# ------------------------------------------------------------------------ cli


@pytest.fixture
def fake_oskard(monkeypatch, tmp_path):
    """Фейк API демона на эфемерном порту; CLI направлен только на него."""
    routes: dict = {}
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self):
            token = self.headers.get("X-Oskar-Token")
            seen.append((self.command, self.path, token))
            if token != "s3cret":
                code, payload = 401, {"detail": "missing or invalid X-Oskar-Token"}
            else:
                code, payload = routes.get((self.command, self.path), (404, {"detail": "not found"}))
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _serve

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token_file = tmp_path / "oskard.token"
    monkeypatch.setattr(cli, "_daemon_api_base", lambda: f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(cli, "_daemon_token_file", lambda: token_file)
    monkeypatch.delenv("OSKAR_API_TOKEN", raising=False)
    yield routes, seen, token_file
    server.shutdown()
    server.server_close()


def run_cli(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["oskar", *argv])
    code = cli.main()
    return code, capsys.readouterr().out


def test_cli_panic_posts_with_token_and_prints_ascii(fake_oskard, monkeypatch, capsys):
    routes, seen, _ = fake_oskard
    routes[("POST", "/panic")] = (200, {"panic": True, "safe_mode": True, "paused": True,
                                        "exchange": {"cancelled": 1, "closed": 2, "errors": []}})
    monkeypatch.setenv("OSKAR_API_TOKEN", "s3cret")
    code, out = run_cli(monkeypatch, capsys, "panic")
    assert code == 0 and seen == [("POST", "/panic", "s3cret")]
    assert "PANIC ENGAGED" in out and "cancelled=1 closed=2 errors=0" in out and out.isascii()


def test_cli_panic_exchange_errors_exit_nonzero_in_ascii(fake_oskard, monkeypatch, capsys):
    routes, seen, _ = fake_oskard
    routes[("POST", "/panic")] = (200, {"panic": True, "safe_mode": True, "paused": True,
                                        "exchange": {"cancelled": 0, "closed": 0,
                                                     "errors": ["d1: позиция не закрыта"]}})
    monkeypatch.setenv("OSKAR_API_TOKEN", "s3cret")
    code, out = run_cli(monkeypatch, capsys, "panic")
    assert code == 1 and "error: d1:" in out and out.isascii()  # cp1251-консоль не получит кириллицу


def test_cli_stale_env_token_falls_back_to_token_file(fake_oskard, monkeypatch, capsys):
    routes, seen, token_file = fake_oskard
    routes[("POST", "/panic/reset")] = (200, {"panic": False, "safe_mode": False, "paused": True})
    token_file.write_text("s3cret\n", encoding="ascii")
    monkeypatch.setenv("OSKAR_API_TOKEN", "old-token")
    code, out = run_cli(monkeypatch, capsys, "panic-reset")
    assert code == 0 and [t for _, _, t in seen] == ["old-token", "s3cret"]
    assert "panic reset: safe_mode=False paused=True" in out


def test_cli_status_prints_json_in_ascii(fake_oskard, monkeypatch, capsys):
    routes, seen, token_file = fake_oskard
    payload = {"run_id": "r1", "paused": False, "last_report": {"note": "кириллица"}}
    routes[("GET", "/status")] = (200, payload)
    token_file.write_text("s3cret", encoding="ascii")
    code, out = run_cli(monkeypatch, capsys, "status")
    assert code == 0 and out.isascii() and json.loads(out) == payload


def test_cli_bypasses_proxy_for_loopback(fake_oskard, monkeypatch, capsys):
    """VPN/прокси в окружении не должен перехватить panic, адресованный демону."""
    routes, seen, token_file = fake_oskard
    routes[("POST", "/panic")] = (200, {"panic": True, "safe_mode": True, "paused": True, "exchange": None})
    token_file.write_text("s3cret", encoding="ascii")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # мертвый прокси
    code, out = run_cli(monkeypatch, capsys, "panic")
    assert code == 0 and seen == [("POST", "/panic", "s3cret")]
    assert "exchange: NOT touched" in out


def test_cli_panic_reports_daemon_down(monkeypatch, capsys, tmp_path):
    with socket.socket() as s:  # свободный порт, на котором никто не слушает
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(cli, "_daemon_api_base", lambda: f"http://127.0.0.1:{port}")
    monkeypatch.setattr(cli, "_daemon_token_file", lambda: tmp_path / "none.token")
    code, out = run_cli(monkeypatch, capsys, "panic")
    assert code == 1 and "OSKARD NOT RUNNING" in out and "PANIC NOT APPLIED" in out


def test_cli_api_address_follows_settings(monkeypatch, tmp_path):
    import oskar.core.daemon as daemon

    (tmp_path / "config").mkdir()
    settings = tmp_path / "config" / "settings.yaml"
    monkeypatch.setattr(daemon, "REPO_ROOT", tmp_path)
    settings.write_text("api:\n  host: localhost\n  port: 9123\n", encoding="utf-8")
    assert cli._daemon_api_base() == "http://localhost:9123"
    settings.write_text("api:\n  host: 0.0.0.0\n", encoding="utf-8")  # не loopback -> дефолт
    assert cli._daemon_api_base() == "http://127.0.0.1:8321"
    settings.write_text("api: [broken", encoding="utf-8")  # битый YAML не роняет panic
    assert cli._daemon_api_base() == "http://127.0.0.1:8321"
