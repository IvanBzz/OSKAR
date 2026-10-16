"""Scout/Trader агенты: стаб-CLI, валидация контрактов, вето, галлюцинации id."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oskar.agents.budget import BudgetExceeded, BudgetLimits
from oskar.agents.scout import ScoutError, analyze_candidate, build_prompt
from oskar.agents.trader import TraderError, decide, resolve_prices
from oskar.contracts.schemas import (
    PriceOption,
    SetupCandidate,
    SetupFeatures,
    SetupVerdict,
)
from oskar.journal.db import connect

LIMITS = BudgetLimits(daily=30, hourly=6)


def candidate() -> SetupCandidate:
    return SetupCandidate(
        id="SOLUSDTUSDT-100-short",
        ts=datetime.now(UTC),
        symbol="SOL/USDT:USDT",
        direction="short",
        setup_type="fvg",
        timeframe="15m",
        score=78,
        features=SetupFeatures(rsi=61.0, trend_mtf="bearish"),
        entry_options=[PriceOption(id="e-market", kind="market", price=100.0),
                       PriceOption(id="e-fvg", kind="limit_fvg_edge", price=101.8)],
        sl_options=[PriceOption(id="s-swing", kind="swing_high", price=103.5)],
        tp_options=[PriceOption(id="t-liq", kind="liquidity_pool", price=95.0),
                    PriceOption(id="t-r0", kind="r_multiple", price=93.0)],
    )


def scout_json(candidate_id="SOLUSDTUSDT-100-short", verdict="take", conf=68):
    return json.dumps({
        "candidate_id": candidate_id,
        "tldr": "Свип хая и ретест FVG сверху; цель — пул ликвидности.",
        "bull_case": "Свип хая и медвежий CHoCH на 15м, ретест FVG в премиуме.",
        "bear_case": "Тренд 4ч еще бычий, шорт против старшего таймфрейма опасен.",
        "bear_rebuttal": "Вход только по лимитке от края FVG с узкой инвалидацией.",
        "verdict": verdict,
        "confidence": conf,
        "knowledge_refs": ["Имбаланс (FVG) валиден пока не перекрыт"],
    }, ensure_ascii=False)


def stub_cmd(tmp_path: Path, payload: str, name: str) -> list[str]:
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import sys, json\n"
        "sys.stdin.buffer.read()\n"
        f"print(json.dumps({{'result': {json.dumps(payload)}, 'model': 'stub'}}))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


@pytest.fixture
def journal(tmp_path):
    conn = connect(tmp_path / "j.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------- scout


def test_scout_take(journal, tmp_path):
    res = analyze_candidate(
        candidate(), journal, "r1", limits=LIMITS,
        cli_cmd=stub_cmd(tmp_path, scout_json(), "ok"),
    )
    assert res.verdict.verdict == "take" and res.verdict.confidence == 68
    row = journal.execute("SELECT agent, decision_id FROM analysis_log").fetchone()
    assert row["agent"] == "scout" and row["decision_id"] == "SOLUSDTUSDT-100-short"


def test_scout_confidence_above_cap_rejected(journal, tmp_path):
    with pytest.raises(ScoutError, match="invalid"):
        analyze_candidate(
            candidate(), journal, "r1", limits=LIMITS,
            cli_cmd=stub_cmd(tmp_path, scout_json(conf=90), "cap"),
        )


def test_scout_wrong_candidate_id_rejected(journal, tmp_path):
    with pytest.raises(ScoutError, match="answered about"):
        analyze_candidate(
            candidate(), journal, "r1", limits=LIMITS,
            cli_cmd=stub_cmd(tmp_path, scout_json(candidate_id="OTHER-1-long"), "wrong"),
        )


def test_scout_budget_checked_before_call(journal, tmp_path):
    with pytest.raises(BudgetExceeded):
        analyze_candidate(
            candidate(), journal, "r1", limits=BudgetLimits(daily=0),
            cli_cmd=stub_cmd(tmp_path, scout_json(), "never"),
        )
    n = journal.execute("SELECT COUNT(*) FROM analysis_log").fetchone()[0]
    assert n == 0  # вызова не было


def test_scout_prompt_has_no_forbidden_stats():
    """Ст. 3: в промпте нет портфельной статистики и prefer-формулировок."""
    p = build_prompt(candidate(), "", "")
    for banned in ("portfolio", "win rate", "prefer short", "prefer long", "WR "):
        assert banned.lower() not in p.lower()


# -------------------------------------------------------------------- trader


def scout_verdict() -> SetupVerdict:
    return SetupVerdict(**json.loads(scout_json()))


def trader_json(**kw):
    base = {"verdict": "execute", "entry_option_id": "e-fvg", "sl_option_id": "s-swing",
            "tp_option_id": "t-liq", "rationale": "лимитка от края FVG, стоп за свингом"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def test_trader_execute(journal, tmp_path):
    res = decide(
        candidate(), scout_verdict(), journal, "r1", limits=LIMITS,
        cli_cmd=stub_cmd(tmp_path, trader_json(), "exec"),
    )
    assert res.decision.verdict == "execute"
    entry, sl, tp, entry_type = resolve_prices(candidate(), res.decision)
    assert (entry, sl, tp) == (101.8, 103.5, 95.0)
    assert entry_type == "limit"


def test_trader_veto(journal, tmp_path):
    payload = json.dumps({
        "verdict": "decline",
        "decline_reason": "вход после того как движение уже случилось, до цели полшага",
        "rationale": "исполнение кривое",
    }, ensure_ascii=False)
    res = decide(
        candidate(), scout_verdict(), journal, "r1", limits=LIMITS,
        cli_cmd=stub_cmd(tmp_path, payload, "veto"),
    )
    assert res.decision.verdict == "decline"
    assert res.decision.entry_option_id is None


def test_trader_prompt_lists_allowed_combos(journal, tmp_path):
    """Блок F (2026-10-16): в промпте — только допустимые комбинации c1..cN с R:R и
    стопом; Trader по-прежнему отвечает тремя id (схема TradeDecision та же)."""
    from types import SimpleNamespace

    from oskar.agents.trader import build_prompt

    c = candidate()
    opt = {o.id: o for o in c.entry_options + c.sl_options + c.tp_options}
    combos = [
        SimpleNamespace(entry=opt["e-fvg"], sl=opt["s-swing"], tp=opt["t-liq"], rr=3.312, sl_pct=1.669),
        SimpleNamespace(entry=opt["e-market"], sl=opt["s-swing"], tp=opt["t-r0"], rr=1.94, sl_pct=3.5),
    ]
    p = build_prompt(c, scout_verdict(), combos)
    assert "## Допустимые комбинации (только из них)" in p
    assert "c1: e-fvg / s-swing / t-liq — R:R 3.31, стоп 1.67%" in p
    assert "c2: e-market / s-swing / t-r0 — R:R 1.94, стоп 3.50%" in p
    assert "{combos}" not in build_prompt(c, scout_verdict())  # без списка — заглушка, не плейсхолдер

    res = decide(c, scout_verdict(), journal, "r1", limits=LIMITS, combos=combos,
                 cli_cmd=stub_cmd(tmp_path, trader_json(), "combos"))
    assert res.decision.entry_option_id == "e-fvg"
    logged = journal.execute("SELECT prompt FROM analysis_log WHERE agent='trader'").fetchone()[0]
    assert "c1: e-fvg / s-swing / t-liq" in logged


def test_trader_hallucinated_option_rejected(journal, tmp_path):
    with pytest.raises(TraderError, match="nonexistent"):
        decide(
            candidate(), scout_verdict(), journal, "r1", limits=LIMITS,
            cli_cmd=stub_cmd(tmp_path, trader_json(entry_option_id="e-imaginary"), "hall"),
        )
