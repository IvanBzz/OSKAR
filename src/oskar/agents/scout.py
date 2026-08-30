"""Scout — LLM-анализ эскалированных кандидатов (Fable/Opus через claude -p).

Дисциплина: бюджет ДО вызова; полный промпт+ответ в analysis_log (ст. 1 п. 4);
выход валидируется контрактом SetupVerdict (confidence <= 75 на уровне схемы);
никакой портфельной статистики в промпте (ст. 3, STATS_MANIFEST).
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from oskar.agents.budget import BudgetLimits, check_budget, log_call
from oskar.agents.claude_cli import AgentCallResult, call_claude, extract_json_block
from oskar.contracts.schemas import SetupCandidate, SetupVerdict

PROMPTS_DIR = Path(__file__).parent / "prompts"
NEUTRAL_CONTEXT = "regime: neutral (Sentinel не запущен — торгуй по структуре, без макро-ставок)"


class ScoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScoutResult:
    verdict: SetupVerdict
    call: AgentCallResult


def _template() -> str:
    text = (PROMPTS_DIR / "scout.md").read_text(encoding="utf-8")
    return text.split("---", 1)[1].strip()  # тело после шапки роль-карты


def build_prompt(
    candidate: SetupCandidate, market_context: str, knowledge: str
) -> str:
    return (
        _template()
        .replace("{candidate_json}", candidate.model_dump_json(indent=2))
        .replace("{market_context}", market_context or NEUTRAL_CONTEXT)
        .replace("{knowledge}", knowledge or "(библиотека недоступна)")
    )


def analyze_candidate(
    candidate: SetupCandidate,
    journal: sqlite3.Connection,
    run_id: str,
    *,
    limits: BudgetLimits,
    knowledge: str = "",
    market_context: str = "",
    model: str | None = None,
    cli_cmd: list[str] | None = None,
) -> ScoutResult:
    """Бросает BudgetExceeded/ScoutError; вызывающий решает деградацию."""
    check_budget(journal, "scout", limits)
    prompt = build_prompt(candidate, market_context, knowledge)
    call = call_claude(prompt, model=model, cli_cmd=cli_cmd)
    log_call(
        journal, run_id=run_id, agent="scout", decision_id=candidate.id,
        model=call.model, prompt_sha=call.prompt_sha, prompt=prompt,
        response=call.text, duration_ms=call.duration_ms,
    )
    try:
        data = extract_json_block(call.text)
        verdict = SetupVerdict(**data)
    except Exception as e:
        raise ScoutError(f"scout output invalid for {candidate.id}: {e}") from e
    if verdict.candidate_id != candidate.id:
        raise ScoutError(
            f"scout answered about {verdict.candidate_id}, asked about {candidate.id}"
        )
    return ScoutResult(verdict=verdict, call=call)


def knowledge_for_candidate(candidate: SetupCandidate, max_chars: int = 2500) -> str:
    """Топ-3 заметки библиотеки по типу сетапа (FTS5; безопасная деградация)."""
    try:
        from oskar.knowledge.index import DEFAULT_DB, build_index, search

        if not DEFAULT_DB.exists():
            build_index()
        terms = {
            "fvg": "FVG имбаланс вход митигация",
            "ob": "ордер-блок вход тело стоп",
            "liquidity_sweep": "свип ликвидности ложный пробой разворот",
            "bos": "BOS слом структуры закрытие",
            "choch": "CHoCH смена характера",
            "confluence": "конфлюэнс сетап чек-лист POI",
        }[candidate.setup_type]
        hits = search(terms, limit=3)
        parts = []
        for h in hits:
            body = Path(h.path if ":" in h.path[:3] else
                        Path(__file__).resolve().parents[3] / h.path
                        ).read_text(encoding="utf-8", errors="replace")
            body = body.split("---", 2)[-1].strip()
            parts.append(f"### {h.title}\n{body[:900]}")
        return "\n\n".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001 - знания опциональны, их отсутствие не блокирует
        return ""
