"""Мост CLI: стаб-процесс вместо реального claude, UTF-8 путь, ретраи, бюджеты."""

import json
import sys
from pathlib import Path

import pytest

from oskar.agents.budget import (
    BudgetExceeded,
    BudgetLimits,
    check_budget,
    log_call,
)
from oskar.agents.claude_cli import (
    AgentCallError,
    call_claude,
    extract_json_block,
    prompt_sha,
)
from oskar.journal.db import connect

STUB_OK = """
import sys, json
prompt = sys.stdin.buffer.read().decode("utf-8")
print(json.dumps({"result": "echo:" + prompt, "model": "stub-model"}))
"""

STUB_FAIL = """
import sys
sys.exit(3)
"""

STUB_GARBAGE = """
print("this is not json")
"""


def stub_cmd(tmp_path: Path, code: str, name: str) -> list[str]:
    script = tmp_path / f"{name}.py"
    script.write_text(code, encoding="utf-8")
    return [sys.executable, str(script)]


def test_call_roundtrip_utf8(tmp_path):
    """Русский промпт проходит без искажений (байтовый stdin, урок v1 cp1251)."""
    cmd = stub_cmd(tmp_path, STUB_OK, "ok")
    res = call_claude("сетап SOL/USDT: имбаланс 15м", cli_cmd=cmd)
    assert res.text == "echo:сетап SOL/USDT: имбаланс 15м"
    assert res.model == "stub-model"
    assert res.prompt_sha == prompt_sha("сетап SOL/USDT: имбаланс 15м")
    assert res.duration_ms >= 0


def test_call_fails_after_retries(tmp_path):
    cmd = stub_cmd(tmp_path, STUB_FAIL, "fail")
    with pytest.raises(AgentCallError, match="exit 3"):
        call_claude("x", cli_cmd=cmd)


def test_call_garbage_output_fails(tmp_path):
    cmd = stub_cmd(tmp_path, STUB_GARBAGE, "garbage")
    with pytest.raises(AgentCallError):
        call_claude("x", cli_cmd=cmd)


def test_extract_json_variants():
    assert extract_json_block('{"a": 1}') == {"a": 1}
    assert extract_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_block('Вот ответ:\n{"a": 1}\nконец') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json_block("никакого json здесь нет")


# ------------------------------------------------------------------- budgets


def test_budget_counts_from_analysis_log(tmp_path):
    j = connect(tmp_path / "j.db")
    limits = BudgetLimits(daily=2, hourly=None)
    check_budget(j, "scout", limits)  # 0 вызовов — ок
    for i in range(2):
        log_call(
            j, run_id="r1", agent="scout", decision_id=f"d{i}", model="m",
            prompt_sha="sha", prompt="p", response="r", duration_ms=10,
        )
    with pytest.raises(BudgetExceeded, match="daily"):
        check_budget(j, "scout", limits)
    # другой агент не задет
    check_budget(j, "trader", limits)
    j.close()


def test_budget_hourly(tmp_path):
    j = connect(tmp_path / "j.db")
    limits = BudgetLimits(daily=100, hourly=1)
    log_call(
        j, run_id="r1", agent="scout", decision_id="d1", model="m",
        prompt_sha="sha", prompt="p", response="r", duration_ms=10,
    )
    with pytest.raises(BudgetExceeded, match="hourly"):
        check_budget(j, "scout", limits)
    j.close()


def test_log_call_persists_full_prompt(tmp_path):
    """Ст. 1 п. 4: полный промпт и ответ восстановимы по prompt_sha."""
    j = connect(tmp_path / "j.db")
    long_prompt = "промпт " * 500
    log_call(
        j, run_id="r1", agent="trader", decision_id="d1", model="fable",
        prompt_sha=prompt_sha(long_prompt), prompt=long_prompt,
        response=json.dumps({"decision": "take"}), duration_ms=1500,
    )
    row = j.execute(
        "SELECT prompt, response FROM analysis_log WHERE prompt_sha = ?",
        (prompt_sha(long_prompt),),
    ).fetchone()
    assert row["prompt"] == long_prompt
    assert "take" in row["response"]
    j.close()
