"""Ревью 2026-10-13, #28/#15: сбои расходуют бюджет, CLI изолирован от секретов демона."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from oskar.agents import claude_cli
from oskar.agents.budget import BudgetExceeded, BudgetLimits, CallBudget, calls_last_hour, calls_today
from oskar.agents.scout import analyze_candidate
from oskar.agents.trader import decide
from oskar.journal.db import connect
from tests.unit.test_scout_trader import candidate, scout_json, scout_verdict, trader_json


def invoke(agent, journal, limits, *, model=None, candidate_id="first"):
    cand = candidate().model_copy(update={"id": candidate_id})
    kwargs = {"limits": limits, "model": model, "cli_cmd": ["fake-claude"]}
    if agent == "scout":
        return analyze_candidate(cand, journal, "r1", **kwargs)
    return decide(cand, scout_verdict(), journal, "r1", **kwargs)


@pytest.mark.parametrize("agent", ["scout", "trader"])
@pytest.mark.parametrize("period", ["daily", "hourly"])
def test_failures_exhaust_budget_across_candidates_and_restart(tmp_path, monkeypatch, agent, period):
    journal = connect(tmp_path / "j.db")
    limits = BudgetLimits(daily=2) if period == "daily" else BudgetLimits(daily=100, hourly=2)
    error = "ошибка " * 500
    calls = []
    prompts = []

    def fail(cmd, **kwargs):
        calls.append(cmd)
        prompts.append(kwargs["input"].decode("utf-8"))
        # Резерв уже закоммичен до запуска процесса и виден после рестарта.
        observer = connect(tmp_path / "j.db")
        assert calls_today(observer, agent) == len(calls)
        observer.close()
        assert not journal.in_transaction
        return subprocess.CompletedProcess(cmd, 3, b"partial answer", error.encode())

    monkeypatch.setattr(claude_cli.subprocess, "run", fail)
    with pytest.raises(claude_cli.AgentCallError, match="exit 3"):
        invoke(agent, journal, limits, model="requested-model")
    rows = journal.execute("SELECT * FROM analysis_log ORDER BY id").fetchall()
    assert len(rows) == 2
    prompt = prompts[0]
    assert prompts == [prompt, prompt] and '"symbol": "SOL/USDT:USDT"' in prompt
    for row in rows:
        assert row["run_id"] == "r1" and row["decision_id"] == "first"
        assert row["prompt"] == prompt and row["prompt_sha"] == claude_cli.prompt_sha(prompt)
        assert row["model"] == "requested-model" and row["duration_ms"] >= 0
        assert error in row["response"] and "partial answer" in row["response"]
    journal.close()

    journal = connect(tmp_path / "j.db")
    with pytest.raises(BudgetExceeded, match=period):
        invoke(agent, journal, limits, candidate_id="second")
    assert len(calls) == 2 and calls_last_hour(journal, agent) == 2
    journal.close()


@pytest.mark.parametrize("agent", ["scout", "trader"])
@pytest.mark.parametrize("limits", [BudgetLimits(daily=1), BudgetLimits(daily=100, hourly=1)])
def test_retry_cannot_exceed_remaining_budget(tmp_path, monkeypatch, agent, limits):
    journal = connect(tmp_path / "j.db")
    calls = []

    def fail(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, b"", b"failed")

    monkeypatch.setattr(claude_cli.subprocess, "run", fail)
    with pytest.raises(BudgetExceeded):
        invoke(agent, journal, limits)
    row = journal.execute("SELECT * FROM analysis_log").fetchone()
    assert len(calls) == 1 and row["model"] == "unknown" and "failed" in row["response"]
    journal.close()


@pytest.mark.parametrize("agent", ["scout", "trader"])
def test_failed_attempt_then_success_are_logged_once_each(tmp_path, monkeypatch, agent):
    journal = connect(tmp_path / "j.db")
    answer = scout_json(candidate_id="first") if agent == "scout" else trader_json()
    replies = iter([
        subprocess.CompletedProcess([], 1, b"", b"first attempt failed"),
        subprocess.CompletedProcess([], 0, json.dumps({"result": answer, "model": "actual"}).encode(), b""),
    ])
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **k: next(replies))
    invoke(agent, journal, BudgetLimits(daily=2), model="requested")
    rows = journal.execute("SELECT * FROM analysis_log ORDER BY id").fetchall()
    assert len(rows) == 2 and "first attempt failed" in rows[0]["response"]
    assert rows[0]["model"] == "requested"
    assert rows[1]["response"] == answer and rows[1]["model"] == "actual"
    assert rows[0]["prompt"] == rows[1]["prompt"]
    journal.close()


@pytest.mark.parametrize("failure", ["timeout", "os", "json", "empty", "array", "cli_error", "missing_cli"])
def test_all_cli_failure_paths_are_persisted(tmp_path, monkeypatch, failure):
    journal = connect(tmp_path / "j.db")
    prompt = "полный снимок " * 500
    budget = CallBudget(journal, "r1", "scout", "d1", BudgetLimits(daily=5))

    def fail(cmd, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 10, output=b"partial response", stderr=b"timeout detail")
        if failure == "os":
            raise OSError("cannot start process")
        outputs = {
            "json": b"malformed response", "empty": b'{"result":""}', "array": b"[]",
            "cli_error": b'{"result":"upstream error", "is_error":true}',
        }
        return subprocess.CompletedProcess(cmd, 0, outputs[failure], b"diagnostic")

    monkeypatch.setattr(claude_cli.subprocess, "run", fail)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    cmd = None if failure == "missing_cli" else ["fake-claude"]
    with pytest.raises(claude_cli.AgentCallError):
        claude_cli.call_claude(prompt, cli_cmd=cmd, budget=budget)
    rows = journal.execute("SELECT * FROM analysis_log").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["model"] == "unknown" and row["prompt"] == prompt
        assert "pending" not in row["response"] and row["duration_ms"] >= 0
        if failure == "timeout":
            assert "partial response" in row["response"] and "timeout detail" in row["response"]
        if failure == "json":
            assert "malformed response" in row["response"]
        if failure == "cli_error":
            assert "upstream error" in row["response"]
    journal.close()


def test_pending_attempt_survives_interruption(tmp_path, monkeypatch):
    journal = connect(tmp_path / "j.db")
    budget = CallBudget(journal, "r1", "scout", "d1", BudgetLimits(daily=1))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(claude_cli.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        claude_cli.call_claude("snapshot", cli_cmd=["fake-claude"], budget=budget)
    journal.close()
    journal = connect(tmp_path / "j.db")
    assert calls_today(journal, "scout") == 1
    with pytest.raises(BudgetExceeded):
        invoke("scout", journal, BudgetLimits(daily=1))
    journal.close()


def test_concurrent_reservations_share_budget(tmp_path):
    journal = connect(tmp_path / "j.db")
    budget = CallBudget(journal, "r1", "scout", "d1", BudgetLimits(daily=1))
    barrier = Barrier(2)

    def reserve():
        barrier.wait(timeout=5)
        try:
            return budget.reserve(model="m", prompt_sha="sha", prompt="p")
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert calls_today(journal, "scout") == 1
    journal.close()


def test_cli_receives_only_allowed_environment_and_neutral_cwd(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    assert claude_cli.AGENT_CWD == repo_root / "data" / "agent_cwd"
    allowed_names = (
        "PATH SYSTEMROOT WINDIR COMSPEC PATHEXT USERPROFILE HOMEDRIVE HOMEPATH HOME APPDATA "
        "LOCALAPPDATA TEMP TMP CLAUDE_CODE_OAUTH_TOKEN HTTP_PROXY HTTPS_PROXY NO_PROXY PYTHONUTF8 "
        "http_proxy https_proxy no_proxy"
    ).split()
    allowed = {key: f"value-{key}" for key in allowed_names}
    secrets = {key: "secret" for key in (
        "BINGX_API_KEY", "BINGX_API_SECRET", "BINGX_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "UNLISTED_SETTING", "PYTHONPATH",
    )}
    monkeypatch.setattr(claude_cli.os, "environ", allowed | secrets)
    cwd = tmp_path / "data" / "agent_cwd"
    monkeypatch.setattr(claude_cli, "AGENT_CWD", cwd)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "claude")

    def run(cmd, **kwargs):
        assert kwargs["env"] == allowed
        assert kwargs["cwd"] == cwd and cwd.is_dir()
        assert kwargs["cwd"] != repo_root
        assert "--disallowedTools" in cmd
        assert claude_cli.DISALLOWED_TOOLS in cmd
        return subprocess.CompletedProcess(cmd, 0, b'{"result":"ok"}', b"")

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    assert claude_cli.call_claude("p").text == "ok"
