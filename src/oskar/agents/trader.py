"""Trader — гейткипер исполнения с правом вето (TradeDecision v2).

Выбирает опции ПО ID из меню кандидата (ст. 2) или отказывается с причиной.
Выбранные id валидируются против меню — галлюцинированный id = ошибка.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from oskar.agents.budget import BudgetLimits, check_budget, log_call
from oskar.agents.claude_cli import AgentCallResult, call_claude, extract_json_block
from oskar.contracts.schemas import SetupCandidate, SetupVerdict, TradeDecision

PROMPTS_DIR = Path(__file__).parent / "prompts"


class TraderError(RuntimeError):
    pass


@dataclass(frozen=True)
class TraderResult:
    decision: TradeDecision
    call: AgentCallResult


def _template() -> str:
    text = (PROMPTS_DIR / "trader.md").read_text(encoding="utf-8")
    return text.split("---", 1)[1].strip()


def _options_line(options) -> str:
    return "; ".join(f"{o.id} ({o.kind} @ {o.price:g}{': ' + o.note if o.note else ''})"
                     for o in options)


def build_prompt(candidate: SetupCandidate, scout_verdict: SetupVerdict) -> str:
    return (
        _template()
        .replace("{candidate_json}", candidate.model_dump_json(indent=2))
        .replace("{scout_verdict}", scout_verdict.model_dump_json(indent=2))
        .replace("{entry_options}", _options_line(candidate.entry_options))
        .replace("{sl_options}", _options_line(candidate.sl_options))
        .replace("{tp_options}", _options_line(candidate.tp_options))
    )


def decide(
    candidate: SetupCandidate,
    scout_verdict: SetupVerdict,
    journal: sqlite3.Connection,
    run_id: str,
    *,
    limits: BudgetLimits,
    model: str | None = None,
    cli_cmd: list[str] | None = None,
) -> TraderResult:
    check_budget(journal, "trader", limits)
    prompt = build_prompt(candidate, scout_verdict)
    call = call_claude(prompt, model=model, cli_cmd=cli_cmd)
    log_call(
        journal, run_id=run_id, agent="trader", decision_id=candidate.id,
        model=call.model, prompt_sha=call.prompt_sha, prompt=prompt,
        response=call.text, duration_ms=call.duration_ms,
    )
    try:
        data = extract_json_block(call.text)
        decision = TradeDecision(
            decision_id=candidate.id,
            ts=datetime.now(UTC),
            candidate_id=candidate.id,
            verdict=data["verdict"],
            entry_option_id=data.get("entry_option_id"),
            sl_option_id=data.get("sl_option_id"),
            tp_option_id=data.get("tp_option_id"),
            rationale=data.get("rationale", ""),
            decline_reason=data.get("decline_reason"),
            model=call.model,
            prompt_sha=call.prompt_sha,
        )
    except Exception as e:
        raise TraderError(f"trader output invalid for {candidate.id}: {e}") from e

    if decision.verdict == "execute":
        menus = {
            "entry": {o.id for o in candidate.entry_options},
            "sl": {o.id for o in candidate.sl_options},
            "tp": {o.id for o in candidate.tp_options},
        }
        chosen = {
            "entry": decision.entry_option_id,
            "sl": decision.sl_option_id,
            "tp": decision.tp_option_id,
        }
        for kind, oid in chosen.items():
            if oid not in menus[kind]:
                raise TraderError(
                    f"trader chose nonexistent {kind} option {oid!r} "
                    f"(menu: {sorted(menus[kind])})"
                )
    return TraderResult(decision=decision, call=call)


def resolve_prices(
    candidate: SetupCandidate, decision: TradeDecision
) -> tuple[float, float, float, str]:
    """Id -> цены (entry, sl, tp) + тип входа. Только для verdict=execute."""
    entry = next(o for o in candidate.entry_options if o.id == decision.entry_option_id)
    sl = next(o for o in candidate.sl_options if o.id == decision.sl_option_id)
    tp = next(o for o in candidate.tp_options if o.id == decision.tp_option_id)
    entry_type = "market" if entry.kind == "market" else "limit"
    return entry.price, sl.price, tp.price, entry_type
