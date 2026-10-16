"""Ядро: API (TestClient), port-lock, shadow-цикл на синтетике."""

import socket
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from oskar.core.api import build_app
from oskar.core.cycle import CycleConfig, ShadowCycle
from oskar.core.daemon import SingleInstanceError, check_port_free
from oskar.core.state import RuntimeState
from oskar.detect.smc import SmcConfig
from oskar.journal.db import connect
from oskar.risk.ladder import Caps, ProfileLimits

CAPS = Caps(
    risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
    confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.3, sl_dist_pct_max=6.0, max_leverage=8)


@pytest.fixture
def state(tmp_path):
    return RuntimeState(journal=connect(tmp_path / "j.db"))


@pytest.fixture
def client(state):
    return TestClient(build_app(state))


def add_pending(state, decision_id="d1", expires_in_min=10):
    expires = (datetime.now(UTC) + timedelta(minutes=expires_in_min)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    state.journal.execute(
        "INSERT INTO pending_approvals (decision_id, card_json, expires_at) VALUES (?,?,?)",
        (decision_id, "{}", expires),
    )
    state.journal.commit()


# ------------------------------------------------------------------------ api


def test_status(client, state):
    r = client.get("/status").json()
    assert r["run_id"] == state.run_id
    assert r["paused"] is False and r["safe_mode"] is False


def test_approve_flow(client, state):
    add_pending(state, "d1")
    assert client.get("/pending").json()[0]["decision_id"] == "d1"
    assert client.post("/decisions/d1/approve").json()["resolution"] == "approved"
    assert client.get("/pending").json() == []
    # повторное разрешение отвергается
    assert client.post("/decisions/d1/reject").status_code == 409


def test_approve_unknown_404(client):
    assert client.post("/decisions/nope/approve").status_code == 404


def test_expired_ttl_fail_closed(client, state):
    add_pending(state, "old", expires_in_min=-5)
    r = client.post("/decisions/old/approve")
    assert r.status_code == 409 and "TTL" in r.json()["detail"]
    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='old'"
    ).fetchone()
    assert row["resolution"] == "expired"


def test_pause_blocks_approvals(client, state):
    add_pending(state, "d2")
    client.post("/pause")
    assert client.post("/decisions/d2/approve").status_code == 423
    assert client.post("/decisions/d2/reject").status_code == 200  # reject разрешен всегда


def test_panic_and_no_auto_resume(client, state):
    r = client.post("/panic").json()
    assert r["panic"] and r["safe_mode"] and r["paused"]
    assert r["exchange"] is None  # монитора в этом тесте нет — биржа не трогалась
    # после panic /resume отказывает — только ручной сброс оператором (ст. 6.3)
    assert client.post("/resume").status_code == 423


# ------------------------------------------------------------------ port lock


def test_port_lock_detects_second_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        with pytest.raises(SingleInstanceError, match="another oskard"):
            check_port_free("127.0.0.1", port)
    finally:
        s.close()
    check_port_free("127.0.0.1", port)  # порт освобожден — проходит


# --------------------------------------------------------------- shadow cycle


def market_df(n: int = 160, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    legs = np.concatenate([
        np.linspace(100, 112, 40), np.linspace(112, 104, 30),
        np.linspace(104, 118, 45), np.linspace(118, 108, 45),
    ])[:n]
    close = legs + rng.normal(0, 0.35, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    ts = 1_700_000_000_000 + np.arange(n) * 900_000
    return pd.DataFrame({
        "ts": ts, "open": open_, "high": np.maximum(open_, close) + spread,
        "low": np.minimum(open_, close) - spread, "close": close,
        "volume": np.abs(rng.normal(1000, 300, n)),
    })


class FakeCache:
    def __init__(self, df):
        self._df = df

    def get(self, symbol, timeframe, limit):
        return self._df.copy()


def make_cycle(state):
    cfg = CycleConfig(
        signal_timeframe="15m", context_timeframes=(),
        min_score=30, smc=SmcConfig(swing_length=5),
    )
    # префикс 130 бар от полной серии — состояние со свежей структурой (см. replay)
    prefix = market_df().iloc[:130].reset_index(drop=True)
    return ShadowCycle(FakeCache(prefix), state, CAPS, PROFILE, cfg)


def test_shadow_cycle_journals_decisions(state):
    cycle = make_cycle(state)
    report = cycle.run_once(["SOL/USDT:USDT"], equity_usd=1000.0)
    assert report.scanned == 1 and not report.errors
    assert report.candidates >= 1
    n_dec = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    n_cf = state.journal.execute("SELECT COUNT(*) FROM counterfactuals").fetchone()[0]
    assert n_dec == n_cf == report.candidates


def test_shadow_cycle_idempotent_within_cooldown(state):
    cycle = make_cycle(state)
    cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    n1 = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    cycle.run_once(["SOL/USDT:USDT"], 1000.0)  # те же свечи — cooldown молчит
    n2 = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert n1 == n2


def test_shadow_cycle_respects_pause(state):
    cycle = make_cycle(state)
    state.paused = True
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    assert report.scanned == 0


def test_live_mode_creates_pending_and_sends_card(state):
    """С card_sink прошедший лестницу кандидат попадает в pending_approvals
    с будущим TTL и уходит карточкой; без sink — чистый shadow (нет pending)."""
    sent = []
    cfg = CycleConfig(
        signal_timeframe="15m", context_timeframes=(),
        min_score=30, smc=SmcConfig(swing_length=5), approval_ttl_seconds=1200,
    )
    prefix = market_df().iloc[:130].reset_index(drop=True)
    cycle = ShadowCycle(
        FakeCache(prefix), state, CAPS, PROFILE, cfg,
        card_sink=lambda cand, verd: sent.append((cand.id, verd.passed)),
    )
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    pending = state.journal.execute(
        "SELECT decision_id, expires_at FROM pending_approvals WHERE resolution IS NULL"
    ).fetchall()
    if report.risk_passed:
        assert sent and sent[0][1] is True
        assert len(pending) == len(sent)
        from datetime import UTC, datetime

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        assert all(p["expires_at"] > now for p in pending)
    else:
        assert not sent and not pending


def test_shadow_mode_never_creates_pending(state):
    cycle = make_cycle(state)  # без card_sink
    cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    n = state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0]
    assert n == 0


def test_card_sink_failure_does_not_break_cycle(state):
    def broken_sink(cand, verd):
        raise ConnectionError("telegram down")

    cfg = CycleConfig(
        signal_timeframe="15m", context_timeframes=(),
        min_score=30, smc=SmcConfig(swing_length=5),
    )
    prefix = market_df().iloc[:130].reset_index(drop=True)
    cycle = ShadowCycle(FakeCache(prefix), state, CAPS, PROFILE, cfg, card_sink=broken_sink)
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    if report.risk_passed:
        # pending создан (TTL fail-closed защитит), ошибка канала — в отчете
        assert any("card_sink" in e for e in report.errors)
        n = state.journal.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0]
        assert n >= 1


def test_best_passing_combo_finds_viable_menu_option(state):
    """Живой урок (12/17 отказов): дефолтная комбинация файлит R:R, но лимитный
    вход с дальним тейком проходит — гейт обязан найти проходную комбинацию."""
    from datetime import UTC, datetime

    from oskar.contracts.schemas import (
        FvgInfo,
        ObInfo,
        PriceOption,
        SetupCandidate,
        SetupFeatures,
    )
    from oskar.core.cycle import best_passing_combo

    tf = "15m"
    cand = SetupCandidate(
        id="T-1-short", ts=datetime.now(UTC), symbol="SOL/USDT:USDT",
        direction="short", setup_type="confluence", timeframe=tf, score=70,
        features=SetupFeatures(
            rsi=55.0, trend_mtf="bearish",
            fvg=FvgInfo(top=102.0, bottom=101.0, mitigated=False, timeframe=tf),
            ob=ObInfo(top=103.0, bottom=102.0, tested=False, timeframe=tf),
        ),
        # market 100 + SL 101.5 + TP 97.5: rr=(2.5-0.16)/1.5=1.56 < 2.0 (файл)
        # limit 101.8 + SL 103.0 + TP 97.5: rr=(4.3-0.163)/1.2=3.45 (проход)
        entry_options=[PriceOption(id="e-market", kind="market", price=100.0),
                       PriceOption(id="e-fvg", kind="limit_fvg_edge", price=101.8)],
        sl_options=[PriceOption(id="s-swing", kind="swing_high", price=101.5),
                    PriceOption(id="s-ob", kind="ob_high", price=103.0)],
        tp_options=[PriceOption(id="t-liq", kind="liquidity_pool", price=97.5)],
    )
    verdict, entry, sl, tp = best_passing_combo(
        cand, CAPS, ProfileLimits(sl_dist_pct_min=0.5, sl_dist_pct_max=3.0, max_leverage=8),
        confidence=68, equity_usd=1000.0,
    )
    assert verdict.passed
    assert (entry, sl) == (101.8, 103.0)  # единственная проходная, не дефолтная
    assert verdict.computed.rr_fee_adjusted > 3.0


# ---------------------------------------------- блок F (2026-10-16): правило выбора


def _short_candidate(sl_options, tp_options, trend="bearish", entries=None):
    from oskar.contracts.schemas import FvgInfo, ObInfo, PriceOption, SetupCandidate, SetupFeatures

    tf = "15m"
    return SetupCandidate(
        id="T-2-short", ts=datetime.now(UTC), symbol="SOL/USDT:USDT",
        direction="short", setup_type="confluence", timeframe=tf, score=70,
        features=SetupFeatures(
            rsi=55.0, trend_mtf=trend,
            fvg=FvgInfo(top=102.0, bottom=101.0, mitigated=False, timeframe=tf),
            ob=ObInfo(top=103.0, bottom=102.0, tested=False, timeframe=tf),
        ),
        entry_options=entries or [PriceOption(id="e-market", kind="market", price=100.0)],
        sl_options=sl_options,
        tp_options=tp_options,
    )


def test_best_passing_combo_takes_min_rr_not_max():
    """Аудит F: максимум R:R брал дальний тейк (медиана прошедших 11.5R, максимум
    107.6R) — counterfactual мерил нереалистичные цели. Берется МИНИМАЛЬНЫЙ R:R над капом."""
    from oskar.contracts.schemas import PriceOption
    from oskar.core.cycle import best_passing_combo

    cand = _short_candidate(
        [PriceOption(id="s-swing", kind="swing_high", price=101.5)],
        [PriceOption(id="t-r1", kind="r_multiple", price=90.0),          # rr 6.53
         PriceOption(id="t-fvg", kind="next_fvg", price=95.0),           # rr 3.20
         PriceOption(id="t-liq", kind="liquidity_pool", price=96.0),     # rr 2.53
         PriceOption(id="t-near", kind="liquidity_pool", price=98.0)],   # rr 1.20 — под капом
    )
    verdict, entry, sl, tp = best_passing_combo(cand, CAPS, PROFILE, confidence=68, equity_usd=1000.0)
    assert verdict.passed and tp == 96.0
    assert verdict.computed.rr_fee_adjusted == pytest.approx(2.533, abs=1e-3)


def test_choice_rule_ties_structural_then_smaller_stop():
    """При равном R:R структурный тейк раньше R-кратного, затем меньший стоп."""
    from oskar.contracts.schemas import PriceOption
    from oskar.risk.combos import evaluate_combos, rank_combos

    tie_kind = _short_candidate(
        [PriceOption(id="s-swing", kind="swing_high", price=101.5)],
        [PriceOption(id="t-r0", kind="r_multiple", price=96.0),
         PriceOption(id="t-liq", kind="liquidity_pool", price=96.0)],
    )
    ranked = rank_combos(evaluate_combos(tie_kind, CAPS, PROFILE, confidence=68, equity_usd=1000.0))
    assert [c.tp.id for c in ranked] == ["t-liq", "t-r0"]

    # SL 102 + TP 94.6 и SL 101 + TP 97.2: оба R:R 2.6 — берется меньший стоп
    tie_rr = _short_candidate(
        [PriceOption(id="s-ob", kind="ob_high", price=102.0),
         PriceOption(id="s-swing", kind="swing_high", price=101.0)],
        [PriceOption(id="t-fvg", kind="next_fvg", price=94.6),
         PriceOption(id="t-liq", kind="liquidity_pool", price=97.2)],
    )
    ranked = rank_combos(evaluate_combos(tie_rr, CAPS, PROFILE, confidence=68, equity_usd=1000.0))
    assert ranked[0].rr == ranked[1].rr == 2.6
    assert (ranked[0].sl.id, ranked[0].tp.id) == ("s-swing", "t-liq")


def test_no_passing_combo_reports_most_advanced_failure():
    """F3: ни одна не прошла — вердикт комбинации, ушедшей по лестнице дальше всех.
    Раньше — дефолтной (первой): журнал писал sl_distance/R:R, хотя резал confluence_gate."""
    from oskar.contracts.schemas import PriceOption
    from oskar.core.cycle import best_passing_combo

    cand = _short_candidate(
        [PriceOption(id="s-tight", kind="swing_high", price=100.1),    # 0.1% — sl_distance
         PriceOption(id="s-swing", kind="swing_high", price=101.5)],
        [PriceOption(id="t-liq", kind="liquidity_pool", price=96.0)],
        trend="bullish",  # шорт против тренда — confluence_gate режет всё
    )
    verdict, entry, sl, tp = best_passing_combo(cand, CAPS, PROFILE, confidence=68, equity_usd=1000.0)
    assert not verdict.passed
    failed = next(s.name for s in verdict.steps if not s.passed)
    assert failed == "confluence_gate"
    assert (entry, sl, tp) == (100.0, 101.5, 96.0)


def test_cycle_writes_escalated_cohorts_with_chosen_prices(state, monkeypatch):
    """F4: pending/executed/error — свои когорты (журнал v4), цены — выбранной комбинации."""
    import oskar.core.cycle as cyc
    from oskar.contracts.schemas import PriceOption
    from oskar.core.escalation import EscalationResult
    from oskar.detect.score import Opportunity

    cand = _short_candidate(
        [PriceOption(id="s-swing", kind="swing_high", price=101.5)],
        [PriceOption(id="t-liq", kind="liquidity_pool", price=96.0)],
    )
    monkeypatch.setattr(cyc, "analyze", lambda df, cfg: None)
    monkeypatch.setattr(cyc, "compute", lambda df: None)
    monkeypatch.setattr(cyc, "multi_tf_trend", lambda per_tf: "bearish")
    monkeypatch.setattr(cyc, "score_opportunity", lambda s, i, m: Opportunity("short", 90, {}))
    cfg = CycleConfig(context_timeframes=(), min_score=30)

    for n, outcome in enumerate(["pending", "executed", "error"]):
        c = cand.model_copy(update={"id": f"T-{n}-short"})
        monkeypatch.setattr(cyc, "build_candidate", lambda *a, _c=c, **k: _c)
        prices = (99.0, 101.0, 95.0) if outcome != "error" else None
        cycle = ShadowCycle(FakeCache(market_df()), state, CAPS, PROFILE, cfg,
                            escalator=lambda _c, o=outcome, p=prices: EscalationResult(outcome=o, prices=p))
        cycle.run_once(["SOL/USDT:USDT"], 1000.0)
        row = state.journal.execute(
            "SELECT cohort, entry_price, sl_price, tp_price FROM counterfactuals WHERE decision_id=?",
            (c.id,),
        ).fetchone()
        assert row["cohort"] == outcome
        # error — цены гейта цикла; pending/executed — комбинации, ушедшей в план
        expected = prices or (100.0, 101.5, 96.0)
        assert (row["entry_price"], row["sl_price"], row["tp_price"]) == expected


def test_shadow_cycle_symbol_error_isolated(state):
    class BrokenCache:
        def get(self, *a):
            raise ConnectionError("boom")

    cfg = CycleConfig(min_score=30, context_timeframes=())
    cycle = ShadowCycle(BrokenCache(), state, CAPS, PROFILE, cfg)
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    assert report.errors and "ConnectionError" in report.errors[0]
