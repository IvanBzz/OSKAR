"""Бюджеты LLM-вызовов per-agent. Источник правды — analysis_log журнала.

Никакого скрытого состояния: лимит проверяется COUNT-запросом по журналу
(переживает рестарты демона). Исчерпание = жесткий стоп вызовов агента
(алерт шлет вызывающий), торговый цикл продолжает детерминированную часть.
"""

import sqlite3
from dataclasses import dataclass


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
) -> None:
    """Полный промпт+ответ в analysis_log (ст. 1 п. 4) — И счетчик бюджета."""
    journal.execute(
        "INSERT INTO analysis_log (run_id, agent, decision_id, model, prompt_sha, "
        "prompt, response, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, agent, decision_id, model, prompt_sha, prompt, response, duration_ms),
    )
    journal.commit()
