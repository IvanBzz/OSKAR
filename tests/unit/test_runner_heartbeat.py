"""Пульс shadow-потока (блок G): строка heartbeat раз в 5 мин независимо от кандидатов.

Строка тика пишется только при кандидатах: на тихом рынке data/oskard.log молчал по
25 мин (15.10), и сторож супервизора (scripts/supervise.ps1, «нет признаков жизни
15 мин») поднимал бы ложное «демон завис». Пульс говорит «поток крутится», а не
«рынок интересный».
"""

import logging

from oskar.core.shadow_runner import HEARTBEAT_SEC, ShadowRunner
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from tests.unit.test_runner_cooldown import FakeCache, FakeClient


class Report:
    def __init__(self, scanned: int, candidates: int):
        self.scanned = scanned
        self.scored = 0
        self.candidates = candidates
        self.risk_passed = 0
        self.errors: list[str] = []


class ScriptedCycle:
    """Цикл с управляемым числом кандидатов (0 = тихий рынок)."""

    def __init__(self):
        self.candidates = 0

    def run_once(self, symbols, equity):
        return Report(len(symbols), self.candidates)

    def resolve_counterfactuals(self, symbol):
        return 0


def make(tmp_path):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    cycle, client = ScriptedCycle(), FakeClient(["A/USDT:USDT", "B/USDT:USDT"])
    t = [0.0]
    runner = ShadowRunner(
        cycle, FakeCache(), client, state,
        top_n=5, min_quote_volume=0, tick_seconds=30, clock=lambda: t[0],
    )
    return runner, cycle, client, t


def heartbeats(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("heartbeat:")]


def test_quiet_market_still_writes_heartbeat_every_5_min(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, client, t = make(tmp_path)
    for step in range(31):  # 15 мин тиков по 30 с без единого кандидата
        t[0] = step * 30.0
        runner.tick()
        runner.heartbeat()
    beats = heartbeats(caplog)
    assert len(beats) == 4  # 0, 5, 10, 15 мин: сторож с порогом 15 мин не сработает
    assert beats[-1] == "heartbeat: tick 31, last candidates нет с запуска"
    # строк тика нет — без пульса лог молчал бы все 15 минут
    assert not [r for r in caplog.records if r.getMessage().startswith("tick ")]


def test_heartbeat_reports_minutes_since_last_candidates(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, client, t = make(tmp_path)
    cycle.candidates = 2
    runner.tick()  # t=0: тик с кандидатами
    cycle.candidates = 0
    assert runner.heartbeat()
    t[0] = HEARTBEAT_SEC - 1
    runner.tick()
    assert not runner.heartbeat()  # раньше 5 мин — второго пульса нет
    t[0] = 2 * HEARTBEAT_SEC
    runner.tick()
    assert runner.heartbeat()
    assert heartbeats(caplog) == [
        "heartbeat: tick 1, last candidates 0 мин назад",
        "heartbeat: tick 3, last candidates 10 мин назад",
    ]


def test_heartbeat_survives_cooldown(tmp_path, caplog):
    """Cooldown kline-API: цикл пропускается, но поток жив — пульс идет."""
    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, client, t = make(tmp_path)
    client.cooldown = 600.0
    runner.tick()
    assert runner.heartbeat()
    assert runner.ticks == 0
    assert heartbeats(caplog) == ["heartbeat: tick 0, last candidates нет с запуска"]


def test_run_forever_beats_after_each_tick(tmp_path, caplog):
    """Пульс — в цикле потока ПОСЛЕ tick(): застрявший тик = нет пульса = алерт супервизора."""
    caplog.set_level(logging.INFO, logger="oskar.shadow")
    runner, cycle, client, t = make(tmp_path)
    order = []

    def tick():
        order.append("tick")
        runner.stop()  # одна итерация цикла

    runner.tick = tick
    runner.run_forever()
    assert order == ["tick"]
    assert len(heartbeats(caplog)) == 1
