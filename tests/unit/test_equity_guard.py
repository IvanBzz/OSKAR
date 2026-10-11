"""Equity: персистентность последнего биржевого значения, источник, блок исполнения."""

from oskar.core.executor import ExecutionOutcome, equity_guarded
from oskar.core.shadow_runner import ShadowRunner
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from tests.unit.test_bootstrap_shadow import FakeCache, FakeClient, FakeCycle


def make_runner(tmp_path, equity_fn, journal=None):
    j = journal or connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    t = [0.0]
    runner = ShadowRunner(
        FakeCycle(), FakeCache(), FakeClient(), state,
        top_n=5, min_quote_volume=0, tick_seconds=60,
        equity_fn=equity_fn, clock=lambda: t[0],
    )
    return runner, state, t


def test_fresh_journal_starts_default(tmp_path):
    runner, *_ = make_runner(tmp_path, equity_fn=None)
    assert runner.equity_source == "default" and runner.equity_usd == 1000.0


def test_exchange_equity_persists_and_survives_restart(tmp_path):
    """Биржа ответила -> источник exchange, значение записано в meta; после
    «рестарта» (новый runner на том же журнале) подхватывается как persisted."""
    runner, state, t = make_runner(tmp_path, equity_fn=lambda: 121.71)
    runner.tick()
    assert runner.equity_source == "exchange" and runner.equity_usd == 121.71
    assert runner.equity_at is not None

    restarted, *_ = make_runner(tmp_path, equity_fn=None, journal=state.journal)
    assert restarted.equity_source == "persisted"
    assert restarted.equity_usd == 121.71  # не $1000 по умолчанию


def test_exchange_failure_downgrades_source(tmp_path):
    """Ключ протух посреди работы: значение остается, источник -> persisted."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return 121.71 if calls["n"] == 1 else None

    runner, state, t = make_runner(tmp_path, equity_fn=flaky)
    runner.tick()
    assert runner.equity_source == "exchange"
    t[0] = 3600.0 + 1  # прошел час — повторный запрос, биржа молчит
    runner.tick()
    assert runner.equity_source == "persisted" and runner.equity_usd == 121.71


def test_guard_blocks_execution_without_exchange_equity():
    executed = []
    guarded = equity_guarded(
        lambda d, m: executed.append((d, m)) or ExecutionOutcome(True, "ok"),
        lambda: "persisted",
    )
    out = guarded("d1", "market")
    assert not out.ok and "заблокировано" in out.message and "BingX" in out.message
    assert executed == []


def test_guard_passes_with_live_equity():
    guarded = equity_guarded(lambda d, m: ExecutionOutcome(True, f"{d}:{m}"), lambda: "exchange")
    assert guarded("d1", "limit").message == "d1:limit"
