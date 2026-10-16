"""Блок I4-I7 (2026-10-16): инварианты в демоне (red -> алерт, повтор не чаще 24 ч), дневная
сводка (текст, расписание 21:00 МСК, /digest), runner не эскалирует под защелкой просадки /
panic, INFO-строки команд бота и отправки карточки."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from oskar.core.emergency import PANIC_META_KEY, SAFE_MODE_META_KEY
from oskar.core.ops_jobs import (
    DIGEST_SENT_KEY,
    INVARIANTS_FIRST_DELAY,
    RED_ALERTS_KEY,
    InvariantWatch,
    OpsScheduler,
    ops_config,
    render_red_alert,
)
from oskar.core.position_monitor import _iso
from oskar.core.shadow_runner import HALT_LOG_SEC, ShadowRunner
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from oskar.ops.digest import RuntimeSnapshot, build_digest
from oskar.ops.invariants import Verdict
from oskar.risk.drawdown import HALT_KEY, PEAK_KEY
from tests.unit.test_emergency import make_poller, message_update, replies
from tests.unit.test_position_monitor import add_trade
from tests.unit.test_runner_cooldown import FakeCache, FakeClient, FakeCycle

NOW = datetime(2026, 10, 16, 18, 0, 30, tzinfo=UTC)  # 21:00:30 МСК


def verdict(vid: str, status: str, detail: str = "детали") -> Verdict:
    return Verdict(id=vid, metric="loser_overhold_count", status=status, value=1.0, n=20,
                   expect="== 0", window="closed_trades=20", detail=detail,
                   description=f"описание {vid}")


class Sink:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, text):
        self.texts.append(text)

        class Sent:
            ok = True

        return Sent()


# ---------------------------------------------------------------- инварианты


def test_red_invariant_alerts_once_per_24h_and_survives_restart(tmp_path):
    j = connect(tmp_path / "j.db")
    sink = Sink()
    verdicts = [verdict("INV-001", "green"), verdict("INV-002", "red", "2 лосера <дольше> & #7")]
    watch = InvariantWatch(j, sink, evaluate_fn=lambda now: verdicts)

    assert watch.check(NOW) == ["INV-002"]
    text = sink.texts[0]
    assert "INV-002" in text and "описание INV-002" in text
    assert "2 лосера &lt;дольше&gt; &amp; #7" in text  # detail экранирован под HTML

    assert watch.check(NOW + timedelta(hours=6)) == []  # тот же red — не чаще раза в сутки
    restarted = InvariantWatch(j, sink, evaluate_fn=lambda now: verdicts)  # рестарт демона
    assert restarted.check(NOW + timedelta(hours=12)) == []
    assert restarted.check(NOW + timedelta(hours=24, minutes=1)) == ["INV-002"]
    assert len(sink.texts) == 2


def test_red_after_green_is_new_again(tmp_path):
    j = connect(tmp_path / "j.db")
    sink = Sink()
    state = {"status": "red"}
    watch = InvariantWatch(j, sink, evaluate_fn=lambda now: [verdict("INV-003", state["status"])])
    assert watch.check(NOW) == ["INV-003"]
    state["status"] = "green"
    assert watch.check(NOW + timedelta(hours=6)) == []
    assert json.loads(j.execute("SELECT value FROM meta WHERE key=?", (RED_ALERTS_KEY,)).fetchone()[0]) == {}
    state["status"] = "red"
    assert watch.check(NOW + timedelta(hours=7)) == ["INV-003"]  # снова red — алерт сразу


def test_undelivered_alert_is_retried(tmp_path):
    j = connect(tmp_path / "j.db")
    calls = []

    def down(text):
        calls.append(text)
        raise ConnectionError("telegram down")

    watch = InvariantWatch(j, down, evaluate_fn=lambda now: [verdict("INV-002", "red")])
    assert watch.check(NOW) == []
    assert watch.check(NOW + timedelta(hours=6)) == [] and len(calls) == 2  # не помечен — повтор


def test_alert_text_shape():
    text = render_red_alert(verdict("INV-004", "red"))
    assert text.startswith("🔴") and "INV-004" in text and "== 0" in text
    assert "авто-revert не выполняется" in text


def test_scheduler_runs_invariants_10_min_after_start_then_every_6h(tmp_path):
    j = connect(tmp_path / "j.db")
    checks = []

    class Watch:
        def check(self, now):
            checks.append(now)
            return []

    start = NOW
    ops = OpsScheduler(j, ops_config({}), watch=Watch(), started_at=start)
    ops.tick(start + timedelta(minutes=5))
    assert checks == []
    ops.tick(start + INVARIANTS_FIRST_DELAY)
    ops.tick(start + INVARIANTS_FIRST_DELAY + timedelta(hours=1))
    assert len(checks) == 1
    ops.tick(start + INVARIANTS_FIRST_DELAY + timedelta(hours=6))
    assert len(checks) == 2


def test_scheduler_survives_failing_check(tmp_path):
    j = connect(tmp_path / "j.db")

    class Watch:
        def check(self, now):
            raise RuntimeError("journal locked")

    ops = OpsScheduler(j, ops_config({}), watch=Watch(), started_at=NOW - timedelta(hours=1))
    ops.tick(NOW)  # не бросает


# ------------------------------------------------------------ расписание сводки


def test_ops_config_defaults_and_validation():
    cfg = ops_config({})
    assert cfg.digest_at.strftime("%H:%M") == "21:00" and cfg.tz_name == "Europe/Moscow"
    assert cfg.invariants_every == timedelta(hours=6) and cfg.tz_label == "МСК"
    custom = ops_config({"ops": {"digest_time": "08:30", "timezone": "UTC", "invariants_every_hours": 2}})
    assert custom.digest_at.strftime("%H:%M") == "08:30" and custom.invariants_every == timedelta(hours=2)
    # 21:00 без кавычек YAML читает как 1260 — не отказ старта, а те же 21:00
    import yaml

    unquoted = yaml.safe_load("ops:\n  digest_time: 21:00\n")
    assert ops_config(unquoted).digest_at.strftime("%H:%M") == "21:00"
    from oskar.core.bootstrap import load_settings

    assert ops_config(load_settings()).digest_at.strftime("%H:%M") == "21:00"  # реальный конфиг
    for bad in ({"digest_time": "25:00"}, {"digest_time": "nine"}, {"invariants_every_hours": 0},
                {"timezone": "Mars/Olympus"}):
        with pytest.raises(ValueError):
            ops_config({"ops": bad})


def test_digest_due_at_21_msk_once_per_day(tmp_path):
    j = connect(tmp_path / "j.db")
    sink = Sink()
    ops = OpsScheduler(j, ops_config({}), watch=None, digest_fn=lambda: "сводка", notify=sink,
                       started_at=NOW - timedelta(hours=3))
    assert not ops.digest_due(datetime(2026, 10, 16, 17, 59, tzinfo=UTC))  # 20:59 МСК
    ops.tick(NOW)
    assert sink.texts == ["сводка"]
    assert j.execute("SELECT value FROM meta WHERE key=?", (DIGEST_SENT_KEY,)).fetchone()[0] == "2026-10-16"
    ops.tick(NOW + timedelta(minutes=1))
    restarted = OpsScheduler(j, ops_config({}), watch=None, digest_fn=lambda: "сводка", notify=sink)
    restarted.tick(NOW + timedelta(minutes=5))  # рестарт после отправки — второй не шлем
    assert sink.texts == ["сводка"]
    assert ops.digest_due(datetime(2026, 10, 17, 18, 1, tzinfo=UTC))  # следующий день


def test_digest_catch_up_after_restart_but_not_next_morning(tmp_path):
    j = connect(tmp_path / "j.db")
    ops = OpsScheduler(j, ops_config({}), watch=None, digest_fn=lambda: "x", notify=Sink())
    assert ops.digest_due(datetime(2026, 10, 16, 18, 40, tzinfo=UTC))  # демон лежал в 21:00 — дослать
    assert not ops.digest_due(datetime(2026, 10, 16, 21, 30, tzinfo=UTC))  # 00:30 МСК — уже нет
    assert not ops.digest_due(datetime(2026, 10, 17, 5, 0, tzinfo=UTC))  # утро — ждем 21:00


# ----------------------------------------------------------------- текст сводки


def _decision(j, did, created, *, passed=True, scout=None, trade=None, outcome="scout_pass"):
    j.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, scout_json, trade_json, risk_json, "
        "outcome, created_at) VALUES ('r1', ?, '{}', ?, ?, ?, ?, ?)",
        (did, json.dumps({"verdict": scout}) if scout else None,
         json.dumps({"verdict": trade}) if trade else None, json.dumps({"passed": passed}),
         outcome, _iso(created)),
    )


def test_digest_text(tmp_path):
    j = connect(tmp_path / "j.db")
    now = datetime.now(UTC)
    h = timedelta(hours=1)
    _decision(j, "rej", now - h, passed=False, outcome="risk_rejected")
    _decision(j, "sp", now - h, scout="pass")
    _decision(j, "dec", now - h, scout="take", trade="decline", outcome="trader_decline")
    _decision(j, "fin", now - h, scout="take", trade="execute", outcome="risk_rejected")
    _decision(j, "err", now - h, outcome="error")
    _decision(j, "old", now - 30 * h, scout="take", trade="execute", outcome="executed")
    add_trade(j, "ex1", opened_at=now - h)  # исполнено и открыто в окне
    add_trade(j, "c1", status="closed", symbol="A/USDT:USDT", opened_at=now - 30 * h)  # вход вчера
    add_trade(j, "c2", status="closed", symbol="B/USDT:USDT", opened_at=now - 30 * h)
    # решения сделок-заглушек — вне окна (воронку считаем по decisions выше)
    j.execute("UPDATE decisions SET created_at=? WHERE decision_id IN ('ex1','c1','c2')",
              (_iso(now - 30 * h),))
    j.execute("UPDATE trades SET realized_pnl=5.5, reconciled=1, closed_at=? WHERE decision_id='c1'",
              (_iso(now - 2 * h),))
    j.execute("UPDATE trades SET realized_pnl=-2.25, reconciled=0, closed_at=? WHERE decision_id='c2'",
              (_iso(now - 2 * h),))
    for agent in ("scout", "scout", "scout", "trader"):
        j.execute("INSERT INTO analysis_log (run_id, agent, model, prompt_sha, prompt, response) "
                  "VALUES ('r1', ?, 'm', 's', 'p', 'r')", (agent,))
    j.execute("INSERT INTO meta(key, value) VALUES (?, '1000.0')", (PEAK_KEY,))
    j.commit()

    snap = RuntimeSnapshot(
        run_id="r1", uptime_sec=5 * 3600 + 12 * 60, runner_ticks=640, safe_mode=True,
        safe_reason="позиция <BERA> & без SL", mode="auto", equity_usd=980.0,
        equity_source="exchange", budgets={"scout": 60, "trader": 30},
    )
    verdicts = [verdict("INV-001", "green"), verdict("INV-002", "red"),
                verdict("INV-003", "insufficient_data"), verdict("INV-004", "insufficient_data")]
    text = build_digest(j, now, snap, verdicts=verdicts, local_now=now, tz_label="МСК")
    lines = text.split("\n")
    assert 8 <= len(lines) <= 12, text
    assert lines[0].startswith("📊 <b>OSKAR: сводка за 24 ч</b>") and "МСК" in lines[0]
    funnel = lines[1]
    for part in ("кандидатов 5", "лестница 4", "Scout take 2 / pass 1", "Trader execute 1",
                 "финальная лестница −1", "исполнено 1", "ошибок/бюджет 1"):
        assert part in funnel, (part, funnel)
    assert "открыто 1, закрыто 2 (+1/−1)" in lines[2] and "сейчас позиций 1" in lines[2]
    assert "сверенный +5.50 $ (1 сд.)" in lines[3] and "несверенный -2.25 $ (1 сд.)" in lines[3]
    assert "P&amp;L" in lines[3]
    assert "Scout 3, остаток 57/60" in lines[4] and "Trader 1, остаток 29/30" in lines[4]
    assert "5ч12м" in lines[5] and "тиков runner'а 640" in lines[5] and "автомат" in lines[5]
    assert "SAFE MODE (позиция &lt;BERA&gt; &amp; без SL)" in lines[6]
    assert "просадка −2.0% от пика $1000.00" in lines[6]
    assert lines[7] == "Инварианты: green 1 · red 1 (INV-002) · insufficient 2"
    assert "<BERA>" not in text  # сырой HTML из причины не проходит


def test_digest_shows_drawdown_halt_and_missing_data(tmp_path):
    j = connect(tmp_path / "j.db")
    j.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
              (HALT_KEY, json.dumps({"equity": 880.0, "peak": 1000.0, "at": "x"})))
    j.commit()
    snap = RuntimeSnapshot(run_id="r1", uptime_sec=60, budgets={"scout": 60, "trader": 30})
    text = build_digest(j, datetime.now(UTC), snap, verdicts=None)
    assert "СТОП ПО ПРОСАДКЕ −12.0%" in text and "Инварианты: не проверены" in text
    assert "тиков runner'а —" in text and "режим: не подключен" in text


def test_digest_command_replies_and_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oskar.buttons")
    poller, state, fake = make_poller(
        tmp_path, [message_update("/digest", update_id=1), message_update("/status", update_id=2)],
        on_digest=lambda: "📊 <b>OSKAR: сводка</b>", on_status=lambda: "OSKAR r1: работает",
    )
    poller.poll_once(timeout=0)
    assert replies(fake) == ["📊 <b>OSKAR: сводка</b>", "OSKAR r1: работает"]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "telegram command /digest (allowlisted chat)" in logged
    assert "telegram command /status (allowlisted chat)" in logged
    assert not any("TOKEN" in m for m in logged)


def test_digest_command_without_digest_and_failing_digest(tmp_path):
    poller, state, fake = make_poller(tmp_path, [message_update("/digest", update_id=1)])
    poller.poll_once(timeout=0)
    assert replies(fake) == ["сводка не подключена"]

    def boom():
        raise RuntimeError("journal <locked>")

    poller2, _, fake2 = make_poller(tmp_path / "b", [message_update("/digest", update_id=1)], on_digest=boom)
    poller2.poll_once(timeout=0)
    assert replies(fake2) == ["сводка недоступна: journal &lt;locked&gt;"]


def test_daemon_digest_fn_builds_text(tmp_path):
    """Сборка в демоне: журнал + состояние runner'а + свежие вердикты инвариантов."""
    from oskar.core.daemon import _make_digest_fn

    state = RuntimeState(journal=connect(tmp_path / "j.db"), run_id="r1")

    class Runner:
        ticks, equity_usd, equity_source = 12, 1000.0, "exchange"

    class Mode:
        def get(self):
            return "confirm"

    class Profile:
        max_loss_hold_seconds = 3 * 3600

    settings = {"agents": {"scout": {"daily_budget": 60}, "trader": {"daily_budget": 30}}}
    text = _make_digest_fn(state, Runner(), Mode(), settings, Profile(), ops_config({}))()
    assert "тиков runner'а 12" in text and "с подтверждением" in text and "МСК" in text
    assert "Инварианты:" in text and "не проверены" not in text
    assert "остаток 60/60" in text


# -------------------------------------------- runner: защелка просадки / panic


class SpyCycle(FakeCycle):
    pass


def _runner(tmp_path):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    cycle = SpyCycle()
    t = [0.0]
    runner = ShadowRunner(cycle, FakeCache(), FakeClient(["A/USDT:USDT"]), state,
                          top_n=5, min_quote_volume=0, tick_seconds=30, clock=lambda: t[0])
    return runner, cycle, state, t


def test_runner_skips_cycle_under_drawdown_halt(tmp_path, caplog):
    """Пока стоит защелка просадки, кандидаты к Scout/Trader не уходят (run_once не зовется);
    строка в лог — раз в 30 мин; защелку сняли — цикл снова идет."""
    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, state, t = _runner(tmp_path)
    j = state.journal
    j.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
              (HALT_KEY, json.dumps({"equity": 880.0, "peak": 1000.0, "at": "x"})))
    j.commit()
    for step in range(int(HALT_LOG_SEC // 30)):  # 30 мин тиков
        t[0] = step * 30.0
        runner.tick()
    assert cycle.runs == []
    skipped = [r for r in caplog.records if "cycle skipped" in r.getMessage()]
    assert len(skipped) == 1 and "просадке" in skipped[0].getMessage()
    assert runner.ticks == int(HALT_LOG_SEC // 30) and "halted" in runner.last_report
    t[0] = HALT_LOG_SEC
    runner.tick()
    assert len([r for r in caplog.records if "cycle skipped" in r.getMessage()]) == 2

    j.execute("DELETE FROM meta WHERE key=?", (HALT_KEY,))  # oskar drawdown-reset
    j.commit()
    t[0] += 30
    runner.tick()
    assert len(cycle.runs) == 1
    assert any("cycle resumed" in r.getMessage() for r in caplog.records)


def test_runner_skips_cycle_under_persisted_panic(tmp_path):
    runner, cycle, state, t = _runner(tmp_path)
    state.journal.execute("INSERT INTO meta(key, value) VALUES (?, 'x')", (PANIC_META_KEY,))
    state.journal.commit()
    runner.tick()
    assert cycle.runs == [] and runner.last_report == {"halted": "panic"}


def test_runner_runs_cycle_without_halt(tmp_path):
    runner, cycle, state, t = _runner(tmp_path)
    state.journal.execute("INSERT INTO meta(key, value) VALUES (?, '{}')", (SAFE_MODE_META_KEY,))
    state.journal.commit()  # SAFE MODE цикл останавливает сам (run_once), runner его не дублирует
    runner.tick()
    assert len(cycle.runs) == 1


# ------------------------------------------------------- лог отправки карточки


def test_card_send_is_logged_without_token(caplog):
    from oskar.core.telegram_out import TelegramSender
    from tests.unit.test_telegram_out import CaptureTransport, candidate
    from tests.unit.test_telegram_out import verdict as risk_verdict

    caplog.set_level(logging.INFO, logger="oskar.telegram")
    sender = TelegramSender("123456789:SECRETSECRETSECRETSECRETSECRET", "1",
                            httpx.Client(transport=CaptureTransport()))
    sender.send_setup_card(candidate(), risk_verdict(), chart_png=None, ttl_minutes=20, buttons=False)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    sent = [m for m in msgs if m.startswith("setup card sent")]
    assert len(sent) == 1 and "message 42" in sent[0] and "buttons=False" in sent[0]
    assert not any("SECRET" in m for m in msgs)
