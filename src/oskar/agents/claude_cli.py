"""Мост к Claude Code CLI (подписка, не API) — `claude -p --output-format json`.

Уроки v1: промпт передается через stdin БАЙТАМИ UTF-8 (subprocess с text=True
кодирует через локальную кодовую страницу — cp1251 ломает русский); никаких
промптов, начинающихся с «You are a trading analyst» (self-detection отказы) —
роль задается естественной формулировкой внутри.

Scout/Trader передают CallBudget: каждая попытка резервируется и сохраняет
полный промпт+ответ в analysis_log (ст. 1 п. 4 — реплей офлайн).
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from oskar.agents.budget import CallBudget

DEFAULT_TIMEOUT_SEC = 180
RETRIES = 1
AGENT_CWD = Path(__file__).resolve().parents[3] / "data" / "agent_cwd"
ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "CLAUDE_CODE_OAUTH_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "PYTHONUTF8",
})


class AgentCallError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentCallResult:
    text: str            # итоговый текст ответа модели
    model: str
    prompt_sha: str
    duration_ms: int
    raw: dict            # полный JSON-ответ CLI (usage и пр.)


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# Scout/Trader отвечают текстом по данным из промпта: инструменты им не нужны,
# а унаследованный профиль прав оператора — поверхность для инъекции через
# текст биржи/модели (ревью 2026-10-13). Запрещаем всё, что читает/пишет/ходит в сеть.
DISALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,Agent,Task"
)


def restricted_tool_args() -> list[str]:
    return ["--disallowedTools", DISALLOWED_TOOLS]


def default_cmd(model: str | None) -> list[str]:
    exe = shutil.which("claude")
    if exe is None:
        raise AgentCallError("claude CLI not found in PATH")
    cmd = [exe, "-p", "--output-format", "json", *restricted_tool_args()]
    if model:
        cmd += ["--model", model]
    return cmd


def call_claude(
    prompt: str,
    *,
    model: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    cli_cmd: list[str] | None = None,
    budget: CallBudget | None = None,
) -> AgentCallResult:
    """Один вызов CLI с ретраем. cli_cmd инжектится в тестах (стаб)."""
    sha = prompt_sha(prompt)
    last_err: Exception | None = None

    for _attempt in range(1 + RETRIES):
        call_id = budget.reserve(model=model or "unknown", prompt_sha=sha, prompt=prompt) if budget else None
        start = time.monotonic()
        stdout = stderr = ""
        try:
            cmd = cli_cmd if cli_cmd is not None else default_cmd(model)
            # Ревью 2026-10-13, #15: CLI не нужны ключи биржи/Telegram и рабочий каталог репозитория.
            AGENT_CWD.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),  # байты! не text=True (cp1251)
                capture_output=True,
                timeout=timeout_sec,
                env={key: value for key, value in os.environ.items() if key.upper() in ENV_ALLOWLIST},
                cwd=AGENT_CWD,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise AgentCallError(f"CLI exit {proc.returncode}")
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise AgentCallError("CLI output is not a JSON object")
            if payload.get("is_error"):
                raise AgentCallError("CLI reported an error")
            text = payload.get("result") or payload.get("text") or ""
            if not isinstance(text, str) or not text:
                raise AgentCallError("empty or invalid result in CLI output")
            result = AgentCallResult(
                text=text,
                model=str(payload.get("model") or model or "unknown"),
                prompt_sha=sha,
                duration_ms=duration_ms,
                raw=payload,
            )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AgentCallError) as e:
            if isinstance(e, subprocess.TimeoutExpired):
                stdout = (e.stdout or b"").decode("utf-8", errors="replace")
                stderr = (e.stderr or b"").decode("utf-8", errors="replace")
            # Ревью 2026-10-13, #28: ошибки без обрезки, включая частичный ответ при timeout (ст. 1 п. 4).
            error_text = f"{type(e).__name__}: {e}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            if budget is not None:
                budget.finish(
                    call_id, model=model or "unknown", response=error_text,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            last_err = AgentCallError(error_text)
        else:
            if budget is not None:
                budget.finish(
                    call_id, model=result.model, response=result.text, duration_ms=result.duration_ms,
                )
            return result
    raise AgentCallError(f"claude CLI failed after {1 + RETRIES} attempts: {last_err}")


def extract_json_block(text: str) -> dict:
    """Достает JSON-объект из текста модели (голый JSON или ```json-блок)."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    if "```" in stripped:
        for chunk in stripped.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                return json.loads(chunk)
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        return json.loads(stripped[start : end + 1])
    raise ValueError("no JSON object found in model output")
