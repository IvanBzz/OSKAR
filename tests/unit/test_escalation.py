"""Эскалация: scout pass / trader veto / финальная лестница / pending+карточка."""

import json
from datetime import UTC, datetime

from oskar.agents.budget import BudgetExceeded
from oskar.contracts.schemas import (
    FvgInfo,
    ObInfo,
    PriceOption,
    SetupCandidate,
    SetupFeatures,
    SetupVerdict,
    TradeDecision,
)
from oskar.core.escalation import Escalator
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from oskar.risk.ladder import Caps, ProfileLimits

CAPS = Caps(
    risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
    confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.5, sl_dist_pct_max=3.0, max_leverage=8)


def candidate() -> SetupCandidate:
    tf = "15m"
    return SetupCandidate(
        id="SOLUSDTUSDT-100-short",
        ts=datetime.now(UTC),
        symbol="SOL/USDT:USDT",
        direction="short",
        setup_type="confluence",
        timeframe=tf,
        score=78,
        features=SetupFeatures(
            rsi=55.0, trend_mtf="bearish",
            fvg=FvgInfo(top=102.0, bottom=101.0, mitigated=False, timeframe=tf),
            ob=ObInfo(top=103.0, bottom=102.0, tested=False, timeframe=tf),
        ),
        entry_options=[PriceOption(id="e-market", kind="market", price=100.0)],
        sl_options=[PriceOption(id="s-swing", kind="swing_high", price=101.5)],
        tp_options=[PriceOption(id="t-liq", kind="liquidity_pool", price=96.0)],
    )


def sv(verdict="take", conf=68) -> SetupVerdict:
    return SetupVerdict(
        candidate_id="SOLUSDTUSDT-100-short",
        tldr="Свип и CHoCH; цель — пул ликвидности снизу.",
        bull_case="Свип и CHoCH, ретест FVG в премиуме, цель — пул ликвидности.",
        bear_case="Тренд старшего ТФ может продолжиться вверх без ретеста.",
        bear_rebuttal="Инвалидация за свингом узкая, риск ограничен профилем.",
        verdict=verdict, confidence=conf,
    )


def td(verdict="execute") -> TradeDecision:
    if verdict == "execute":
        return TradeDecision(
            decision_id="SOLUSDTUSDT-100-short", ts=datetime.now(UTC),
            candidate_id="SOLUSDTUSDT-100-short", verdict="execute",
            entry_option_id="e-market", sl_option_id="s-swing", tp_option_id="t-liq",
            rationale="market вход, стоп за свингом", model="stub", prompt_sha="x" * 64,
        )
    return TradeDecision(
        decision_id="SOLUSDTUSDT-100-short", ts=datetime.now(UTC),
        candidate_id="SOLUSDTUSDT-100-short", verdict="decline",
        rationale="кривое исполнение",
        decline_reason="стоп в скоплении стопов толпы, вход после движения",
        model="stub", prompt_sha="x" * 64,
    )


def resolve_fn(c, d):
    entry = next(o for o in c.entry_options if o.id == d.entry_option_id)
    sl = next(o for o in c.sl_options if o.id == d.sl_option_id)
    tp = next(o for o in c.tp_options if o.id == d.tp_option_id)
    return entry.price, sl.price, tp.price, "market"


def make(tmp_path, scout, trader, cards=None):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    esc = Escalator(
        state, CAPS, PROFILE,
        scout_fn=scout, trader_fn=trader, resolve_fn=resolve_fn,
        approval_ttl_seconds=1200,
        card_sink=(lambda c, v, s: cards.append((c.id, s.confidence))) if cards is not None else None,
        equity_fn=lambda: 1000.0,
    )
    return esc, state


def test_full_path_to_pending_with_card(tmp_path):
    cards = []
    esc, state = make(tmp_path, lambda c: sv(), lambda c, v: td(), cards)
    res = esc(candidate())
    assert res.outcome == "pending"
    assert cards == [("SOLUSDTUSDT-100-short", 68)]
    row = state.journal.execute(
        "SELECT decision_id, expires_at FROM pending_approvals"
    ).fetchone()
    assert row["decision_id"] == "SOLUSDTUSDT-100-short"
    assert json.loads(res.scout_json)["verdict"] == "take"
    assert json.loads(res.trade_json)["verdict"] == "execute"


def test_scout_pass_stops_pipeline(tmp_path):
    def no_trader(c, v):
        raise AssertionError("trader must not be called after scout pass")

    esc, state = make(tmp_path, lambda c: sv(verdict="pass"), no_trader)
    res = esc(candidate())
    assert res.outcome == "scout_pass"
    n = state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0]
    assert n == 0


def test_trader_veto(tmp_path):
    esc, state = make(tmp_path, lambda c: sv(), lambda c, v: td("decline"))
    res = esc(candidate())
    assert res.outcome == "trader_decline"
    assert "стопов толпы" in json.loads(res.trade_json)["decline_reason"]


def test_final_ladder_rechecks_chosen_prices(tmp_path):
    """Ст. 2: даже после execute выбранные цены идут через полную лестницу.
    SHORT с TP 99.0 дает fee-adjusted RR < 2.0 -> отказ на финальной проверке."""
    bad = candidate().model_copy(update={
        "tp_options": [PriceOption(id="t-liq", kind="liquidity_pool", price=99.0)]
    })
    esc, state = make(tmp_path, lambda c: sv(), lambda c, v: td())
    res = esc(bad)
    assert res.outcome == "risk_rejected"
    assert "rr_fee_adjusted" in res.detail


def test_scout_budget_degrades_to_error(tmp_path):
    def broke(c):
        raise BudgetExceeded("scout: daily budget exhausted (30/30)")

    esc, state = make(tmp_path, broke, lambda c, v: td())
    res = esc(candidate())
    assert res.outcome == "error" and "budget" in res.detail


def test_card_failure_keeps_pending(tmp_path):
    def bad_card(c, v, s):
        raise ConnectionError("telegram down")

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    esc = Escalator(
        state, CAPS, PROFILE, scout_fn=lambda c: sv(), trader_fn=lambda c, v: td(),
        resolve_fn=resolve_fn, approval_ttl_seconds=1200, card_sink=bad_card,
        equity_fn=lambda: 1000.0,
    )
    res = esc(candidate())
    assert res.outcome == "pending"  # TTL fail-closed прикроет недоставку
    n = state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0]
    assert n == 1
