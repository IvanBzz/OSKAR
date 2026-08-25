"""Мост к Claude Code CLI (подписка, не API) — `claude -p --output-format json`.

Уроки v1: промпт передается через stdin БАЙТАМИ UTF-8 (subprocess с text=True
кодирует через локальную кодовую страницу — cp1251 ломает русский); никаких
промптов, начинающихся с «You are a trading analyst» (self-detection отказы) —
роль задается естественной формулировкой внутри.

Каждый вызов возвращает prompt_sha; вызывающий ОБЯЗАН записать полный
промпт+ответ в analysis_log (ст. 1 п. 4 — реплей офлайн).
"""

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT_SEC = 180
RETRIES = 1


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


def default_cmd(model: str | None) -> list[str]:
    exe = shutil.which("claude")
    if exe is None:
        raise AgentCallError("claude CLI not found in PATH")
    cmd = [exe, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    return cmd


def call_claude(
    prompt: str,
    *,
    model: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    cli_cmd: list[str] | None = None,
) -> AgentCallResult:
    """Один вызов CLI с ретраем. cli_cmd инжектится в тестах (стаб)."""
    cmd = cli_cmd if cli_cmd is not None else default_cmd(model)
    sha = prompt_sha(prompt)
    last_err: Exception | None = None

    for _attempt in range(1 + RETRIES):
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),  # байты! не text=True (cp1251)
                capture_output=True,
                timeout=timeout_sec,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            if proc.returncode != 0:
                raise AgentCallError(
                    f"CLI exit {proc.returncode}: "
                    f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
                )
            payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
            text = payload.get("result") or payload.get("text") or ""
            if not text:
                raise AgentCallError(f"empty result in CLI output: {str(payload)[:300]}")
            return AgentCallResult(
                text=text,
                model=str(payload.get("model", model or "unknown")),
                prompt_sha=sha,
                duration_ms=duration_ms,
                raw=payload,
            )
        except (subprocess.TimeoutExpired, json.JSONDecodeError, AgentCallError) as e:
            last_err = e
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
