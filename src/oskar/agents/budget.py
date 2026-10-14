"""Бюджеты LLM-вызовов per-agent. Источник правды — analysis_log журнала.

Никакого скрытого состояния: лимит проверяется COUNT-запросом по журналу
(переживает рестарты демона). Исчерпание = жесткий стоп вызовов агента
(алерт шлет вызывающий), торговый цикл продолжает детерминированную часть.
"""

import sqlite3
from dataclasses import dataclass

from oskar.journal.db import transaction


@dataclass(frozen=True)
class BudgetLimits:
    daily: int
    hourly: int | None = None


class BudgetExceeded(RuntimeError):
    pass


def calls_today(journal: sqlite3.Connection, agent: str) -> int:
    row = journal.execute(
        "SELECT COUNT(*) FROM analysis_log WHERE agent = ? "
        "AND created_at >= strftime('%Y-%m-%dT00:00:00Z','now')",
        (agent,),
    ).fetchone()
    return int(row[0])


def calls_last_hour(journal: sqlite3.Connection, agent: str) -> int:
    row = journal.execute(
        "SELECT COUNT(*) FROM analysis_log WHERE agent = ? "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour')",
        (agent,),
    ).fetchone()
    return int(row[0])


def check_budget(journal: sqlite3.Connection, agent: str, limits: BudgetLimits) -> None:
    """Бросает BudgetExceeded ДО вызова LLM. Порядок: сначала дневной лимит."""
    day = calls_today(journal, agent)
    if day >= limits.daily:
        raise BudgetExceeded(f"{agent}: daily budget exhausted ({day}/{limits.daily})")
    if limits.hourly is not None:
        hour = calls_last_hour(journal, agent)
        if hour >= limits.hourly:
            raise BudgetExceeded(f"{agent}: hourly budget exhausted ({hour}/{limits.hourly})")


def log_call(
    journal: sqlite3.Connection,
    *,
    run_id: str,
    agent: str,
    decision_id: str | None,
    model: str,
    prompt_sha: str,
    prompt: str,
    response: str,
    duration_ms: int,
) -> int:
    """Полный промпт+ответ в analysis_log (ст. 1 п. 4) — И счетчик бюджета."""
    with transaction(journal):
        cursor = journal.execute(
            "INSERT INTO analysis_log (run_id, agent, decision_id, model, prompt_sha, "
            "prompt, response, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, agent, decision_id, model, prompt_sha, prompt, response, duration_ms),
        )
    return int(cursor.lastrowid)


@dataclass(frozen=True)
class CallBudget:
    journal: sqlite3.Connection
    run_id: str
    agent: str
    decision_id: str | None
    limits: BudgetLimits

    def reserve(self, *, model: str, prompt_sha: str, prompt: str) -> int:
        """Ревью 2026-10-13, #28: сбой/рестарт и каждый ретрай тоже расходуют бюджет."""
        with transaction(self.journal):
            # Проверка и резерв атомарны для других соединений; сеть идет после commit.
            if not self.journal.in_transaction:
                self.journal.execute("BEGIN IMMEDIATE")
            check_budget(self.journal, self.agent, self.limits)
            return log_call(
                self.journal, run_id=self.run_id, agent=self.agent, decision_id=self.decision_id,
                model=model, prompt_sha=prompt_sha, prompt=prompt,
                response="CLI attempt pending (no response recorded yet)", duration_ms=0,
            )

    def finish(self, call_id: int, *, model: str, response: str, duration_ms: int) -> None:
        """Обновляем резерв, а не добавляем второй вызов; полный ответ нужен для реплея."""
        with transaction(self.journal):
            self.journal.execute(
                "UPDATE analysis_log SET model=?, response=?, duration_ms=? WHERE id=?",
                (model, response, duration_ms, call_id),
            )
