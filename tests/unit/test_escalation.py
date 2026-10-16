"""Эскалация: scout pass / trader veto / финальная лестница / pending+карточка."""

import json
from datetime import UTC, datetime

import pytest

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
        card_sink=(
            (lambda c, v, s, p=None: cards.append((c.id, s.confidence, p)))
            if cards is not None else None
        ),
        equity_fn=lambda: 1000.0,
    )
    return esc, state


def test_full_path_to_pending_with_card(tmp_path):
    cards = []
    esc, state = make(tmp_path, lambda c: sv(), lambda c, v: td(), cards)
    res = esc(candidate())
    assert res.outcome == "pending"
    assert len(cards) == 1
    card_id, conf, plan = cards[0]
    assert (card_id, conf) == ("SOLUSDTUSDT-100-short", 68)
    # в pending лежит готовый план исполнения, не сырой кандидат
    from oskar.contracts.schemas import ExecutionPlan

    row = state.journal.execute(
        "SELECT decision_id, card_json, expires_at FROM pending_approvals"
    ).fetchone()
    assert row["decision_id"] == "SOLUSDTUSDT-100-short"
    stored = ExecutionPlan.model_validate_json(row["card_json"])
    assert stored == plan
    assert stored.qty > 0 and stored.leverage >= 1 and stored.sl == 101.5
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
    SHORT с TP 99.0 дает fee-adjusted RR < 2.0 -> отказ (с 2026-10-16 — еще на переборе
    меню до Trader'а: no_passing_combo; финал без перебора — test_final_ladder_still_rejects)."""
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


# ----------------------------------------------- блок F (2026-10-16): воронка сетапов


def td_ids(entry="e-market", sl="s-swing", tp="t-liq") -> TradeDecision:
    return td().model_copy(update={"entry_option_id": entry, "sl_option_id": sl, "tp_option_id": tp})


def funnel_candidate() -> SetupCandidate:
    """short @100: s-tight (0.1%) режет sl_distance; s-swing проходит с обоими тейками."""
    return candidate().model_copy(update={
        "sl_options": [PriceOption(id="s-swing", kind="swing_high", price=101.5),
                       PriceOption(id="s-tight", kind="swing_high", price=100.1)],
        "tp_options": [PriceOption(id="t-r1", kind="r_multiple", price=90.0),
                       PriceOption(id="t-liq", kind="liquidity_pool", price=96.0)],
    })


def test_trader_sees_only_passing_combos_in_rule_order(tmp_path):
    """F1: Trader получает только проходные комбинации — от минимального R:R."""
    seen = {}

    def trader(c, v, combos=None):
        seen["combos"] = combos
        return td_ids()

    esc, state = make(tmp_path, lambda c: sv(), trader)
    res = esc(funnel_candidate())
    assert res.outcome == "pending" and res.prices == (100.0, 101.5, 96.0)
    combos = seen["combos"]
    assert [c.label for c in combos] == ["e-market/s-swing/t-liq", "e-market/s-swing/t-r1"]
    assert all(c.passed for c in combos)
    assert combos[0].rr < combos[1].rr and combos[0].sl_pct == pytest.approx(1.5)


def test_trader_menu_capped_at_eight(tmp_path):
    from oskar.core.escalation import MAX_TRADER_COMBOS

    seen = {}

    def trader(c, v, combos=None):
        seen["combos"] = combos
        return td_ids(tp="t-0")

    many = candidate().model_copy(update={"tp_options": [
        PriceOption(id=f"t-{i}", kind="liquidity_pool", price=96.0 - i * 0.5) for i in range(12)
    ]})
    esc, state = make(tmp_path, lambda c: sv(), trader)
    assert esc(many).outcome == "pending"
    rrs = [c.rr for c in seen["combos"]]
    assert len(rrs) == MAX_TRADER_COMBOS == 8 and rrs == sorted(rrs)
    assert seen["combos"][0].tp.id == "t-0"  # ближайшая цель первой


def test_no_passing_combo_skips_trader(tmp_path):
    """F1: проходных нет — Trader не вызывается (бюджет), причина — шаг лестницы."""
    def no_trader(c, v, combos=None):
        raise AssertionError("trader must not be called without passing combos")

    bad = candidate().model_copy(update={
        "tp_options": [PriceOption(id="t-liq", kind="liquidity_pool", price=99.0)]
    })
    esc, state = make(tmp_path, lambda c: sv(), no_trader)
    res = esc(bad)
    assert res.outcome == "risk_rejected"
    assert res.detail == "no_passing_combo: rr_fee_adjusted"
    assert res.trade_json is None and res.prices is None


def test_no_passing_combo_uses_scout_confidence(tmp_path):
    """Перебор идет с confidence Scout'а: 55 < 60 — ни одна комбинация не проходит."""
    esc, state = make(tmp_path, lambda c: sv(conf=55), lambda c, v, combos=None: td_ids())
    res = esc(candidate())
    assert res.outcome == "risk_rejected" and res.detail == "no_passing_combo: confidence_bounds"


def test_trader_choice_failing_ladder_falls_back_with_mark(tmp_path):
    """F1: выбор Trader'а не прошел финальную лестницу — проходная по единому правилу,
    пометка combo_fallback в detail, в плане и в карточке; цены — взятой комбинации."""
    from oskar.contracts.schemas import ExecutionPlan

    cards = []

    def sink(c, v, s, p=None, note=None, buttons=True):
        cards.append((p, note, buttons))
        return 7

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    esc = Escalator(
        state, CAPS, PROFILE, scout_fn=lambda c: sv(),
        trader_fn=lambda c, v, combos=None: td_ids(sl="s-tight", tp="t-r1"),
        resolve_fn=resolve_fn, approval_ttl_seconds=1200, card_sink=sink, equity_fn=lambda: 1000.0,
    )
    res = esc(funnel_candidate())
    mark = "combo_fallback: e-market/s-tight/t-r1 -> e-market/s-swing/t-liq"
    assert res.outcome == "pending" and res.detail == mark
    assert res.prices == (100.0, 101.5, 96.0)
    plan = ExecutionPlan.model_validate_json(state.journal.execute(
        "SELECT card_json FROM pending_approvals").fetchone()[0])
    assert (plan.sl, plan.tp) == (101.5, 96.0) and plan.tldr.startswith(mark)
    assert cards[0][1] == mark and cards[0][2] is True
    # выбор Trader'а в журнале остается как был — подмена видна, а не переписана
    assert json.loads(res.trade_json)["sl_option_id"] == "s-tight"


def test_final_ladder_still_rejects_when_world_changed(tmp_path):
    """Ст. 2: финальная проверка остается. Портфель заполнился, пока думал Trader, —
    отказ, fallback не помогает (лимит позиций у всех комбинаций общий)."""
    from oskar.risk.ladder import OpenPosition

    calls = []

    def positions():
        calls.append(1)
        if len(calls) == 1:
            return []
        return [OpenPosition(symbol=f"X{i}/USDT:USDT", side="long", risk_usd=1.0) for i in range(3)]

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    esc = Escalator(
        state, CAPS, PROFILE, scout_fn=lambda c: sv(),
        trader_fn=lambda c, v, combos=None: td_ids(), resolve_fn=resolve_fn,
        approval_ttl_seconds=1200, open_positions_fn=positions, equity_fn=lambda: 1000.0,
    )
    res = esc(candidate())
    assert res.outcome == "risk_rejected" and res.detail == "final_ladder: max_open_positions"
    n = state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0]
    assert n == 0


def test_trader_cross_pair_triple_falls_back(tmp_path):
    """Ревью Codex 2026-10-16 (P2): стоп одной пары + R-тейк другой — id существуют и
    лестница такую тройку пропускает (R:R ~5.2), но перебор ее исключил -> fallback."""
    cand = candidate().model_copy(update={
        "sl_options": [PriceOption(id="s-swing", kind="swing_high", price=101.5),
                       PriceOption(id="s-b", kind="swing_high", price=102.5)],
        "tp_options": [PriceOption(id="t-liq", kind="liquidity_pool", price=96.0),
                       PriceOption(id="t-r1-market-b", kind="r_multiple", price=92.0)],
    })
    esc, state = make(tmp_path, lambda c: sv(),
                      lambda c, v, combos=None: td_ids(sl="s-swing", tp="t-r1-market-b"))
    res = esc(cand)
    assert res.outcome == "pending"
    assert res.detail == "combo_fallback: e-market/s-swing/t-r1-market-b -> e-market/s-swing/t-liq"
    assert res.prices == (100.0, 101.5, 96.0)
