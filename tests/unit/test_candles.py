"""Блок J: теневой детектор свечных паттернов.

Эталонные свечи (позитив/негатив) на каждый паттерн, границы порогов, сверка с pandas-ta,
контекст (экстремум, движение в ATR), отсутствие look-ahead и главное — инвариант ст. 5:
тень не меняет торгового поведения (candle_pattern пуст, вердикт лестницы, журнал цикла и
промпты Scout/Trader те же, что без блока J, бит-в-бит).
"""

import json
import logging
import re
from typing import get_args

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from oskar.agents import scout as scout_mod
from oskar.agents import trader as trader_mod
from oskar.contracts.schemas import (
    PROMPT_EXCLUDE,
    CandleFrame,
    CandlePattern,
    CandleShadow,
    SetupCandidate,
)
from oskar.core import cycle as cycle_mod
from oskar.core.cycle import CycleConfig, ShadowCycle
from oskar.core.escalation import EscalationResult
from oskar.core.state import RuntimeState
from oskar.detect import candles
from oskar.detect.candles import (
    CANDLES_VERSION,
    DEFAULT_CONFIG,
    PATTERNS,
    CandleConfig,
    detect_patterns,
    label_frame,
    pattern_context,
    shadow_labels,
    shadow_or_none,
)
from oskar.detect.smc import SmcConfig
from oskar.journal.db import connect
from oskar.risk.ladder import evaluate
from tests.unit.test_core import CAPS as CORE_CAPS
from tests.unit.test_core import PROFILE as CORE_PROFILE
from tests.unit.test_core import market_df
from tests.unit.test_ladder import CAPS as LADDER_CAPS
from tests.unit.test_ladder import PROFILE as LADDER_PROFILE
from tests.unit.test_ladder import candidate as ladder_candidate
from tests.unit.test_ladder import features_ok, good_input
from tests.unit.test_scout_trader import candidate as agent_candidate
from tests.unit.test_scout_trader import scout_verdict

T0 = 1_700_000_000_000
M15 = 900_000
SYM = "SOL/USDT:USDT"
V1_FEATURE_KEYS = {"rsi", "atr_pct", "trend_mtf", "fvg", "ob", "swept_liquidity", "candle_pattern"}


def bars(*rows, step_ms: int = M15) -> pd.DataFrame:
    """rows: (open, high, low, close); ts — открытие свечи."""
    return pd.DataFrame([
        {"ts": T0 + k * step_ms, "open": o, "high": h, "low": lo, "close": c, "volume": 1000.0}
        for k, (o, h, lo, c) in enumerate(rows)
    ])


def found(*rows, cfg: CandleConfig = DEFAULT_CONFIG) -> list[str]:
    return detect_patterns(bars(*rows), cfg=cfg)


# ------------------------------------------------------------ эталонные свечи

# Размечается последняя свеча. Поглощение и звезды — без гэпа (open = close предыдущей).
POSITIVE = {
    "doji": [(100, 102, 98, 100.2)],                       # тело 5%
    "dragonfly_doji": [(101.5, 102, 92, 101.8)],           # верхняя тень 2%
    "gravestone_doji": [(100.5, 110, 100, 100.2)],         # нижняя тень 2%
    "pin_bar_bull": [(100, 101.5, 97, 101)],               # хвост 3 тела, нос 11%
    "pin_bar_bear": [(101, 104, 99.5, 100)],
    "engulfing_bull": [(101, 101.2, 99.8, 100), (100, 101.6, 99.9, 101.5)],
    "engulfing_bear": [(100, 101.2, 99.8, 101), (101, 101.1, 99.4, 99.5)],
    "inside_bar": [(100, 105, 95, 102), (101, 104, 96, 102.5)],
    "harami_bull": [(104, 104.5, 95.5, 96), (99, 101.5, 98.5, 101)],
    "harami_bear": [(96, 104.5, 95.5, 104), (101, 101.5, 98.5, 99)],
    "morning_star": [(110, 110.5, 99.5, 100), (100, 101, 98.5, 100.5), (100.5, 106.5, 100.2, 106)],
    "evening_star": [(100, 110.5, 99.5, 110), (110, 111.5, 109, 109.5), (109.5, 109.8, 103.5, 104)],
    "marubozu_bull": [(100, 110.5, 99.6, 110)],            # тело 91.7%
    "marubozu_bear": [(110, 110.4, 99.5, 100)],
}

# Ближайшие промахи: одно условие паттерна не выполнено.
NEGATIVE = {
    "doji": [(100, 102, 98, 100.5)],                       # тело 12.5% > 10%
    "dragonfly_doji": [(101.5, 103.5, 92, 101.8)],         # верхняя тень 14.8% > 10%
    "gravestone_doji": [(100.5, 110, 98.5, 100.2)],        # нижняя тень 14.8% > 10%
    "pin_bar_bull": [(100, 101.5, 98.1, 101)],             # хвост 1.9 < 2 тел
    "pin_bar_bear": [(101, 102.9, 99.5, 100)],             # хвост 1.9 < 2 тел
    "engulfing_bull": [(101, 101.2, 99.8, 100), (100, 101.2, 99.9, 100.9)],  # не накрыло open первой
    "engulfing_bear": [(100, 101.2, 99.8, 101), (101, 101.1, 99.9, 100.1)],
    "inside_bar": [(100, 105, 95, 102), (101, 105, 95, 102.5)],  # тот же диапазон — не внутри
    "harami_bull": [(104, 104.5, 95.5, 96), (98, 102.5, 97.5, 102.5)],  # тело 4.5 > половины 8
    "harami_bear": [(96, 104.5, 95.5, 104), (102.5, 102.5, 97.5, 98)],
    "morning_star": [(110, 110.5, 99.5, 100), (100, 101, 98.5, 100.5), (100.5, 106.5, 100.2, 104.9)],
    "evening_star": [(100, 110.5, 99.5, 110), (110, 111.5, 109, 109.5), (109.5, 109.8, 103.5, 105.1)],
    "marubozu_bull": [(100, 110, 99.9, 109)],              # 9 / 10.1 < 90%
    "marubozu_bear": [(109, 110, 99.9, 100)],
}


def test_reference_cases_cover_the_whole_menu():
    assert PATTERNS == get_args(CandlePattern)
    assert set(POSITIVE) == set(NEGATIVE) == set(PATTERNS)


@pytest.mark.parametrize("pattern", list(POSITIVE))
def test_pattern_positive(pattern):
    assert pattern in found(*POSITIVE[pattern])


@pytest.mark.parametrize("pattern", list(NEGATIVE))
def test_pattern_negative(pattern):
    assert pattern not in found(*NEGATIVE[pattern])


def test_output_is_menu_ordered_and_exact():
    assert found(*POSITIVE["dragonfly_doji"]) == ["doji", "dragonfly_doji", "pin_bar_bull"]
    assert found(*POSITIVE["gravestone_doji"]) == ["doji", "gravestone_doji", "pin_bar_bear"]
    assert found(*POSITIVE["doji"]) == ["doji"]
    assert found(*POSITIVE["pin_bar_bull"]) == ["pin_bar_bull"]


def test_flat_bar_has_no_shape():
    assert found((100, 100, 100, 100)) == []


def test_pin_bar_is_shape_not_color():
    """Медвежья свеча с длинным нижним хвостом — тот же молот по форме."""
    assert "pin_bar_bull" in found((101, 101.5, 97, 100))


def test_engulfing_needs_opposite_color_and_bigger_body():
    same_color = [(100, 101.2, 99.8, 101), (100.5, 102.6, 99.9, 102.5)]
    assert "engulfing_bull" not in found(*same_color)
    equal_bodies = [(101, 101.2, 99.8, 100), (100, 101.1, 99.9, 101)]
    assert "engulfing_bull" not in found(*equal_bodies)


def test_harami_cross_and_body_outside():
    assert {"harami_bull", "doji"} <= set(found((104, 104.5, 95.5, 96), (100, 101, 99, 100.05)))
    assert "harami_bull" not in found((104, 104.5, 95.5, 96), (103, 104.6, 102, 104.4))


def test_star_first_bear_required():
    bull_first = [(100, 110.5, 99.5, 110), (100, 101, 98.5, 100.5), (100.5, 106.5, 100.2, 106)]
    assert "morning_star" not in found(*bull_first)


def test_star_must_sit_below_middle_of_first_body():
    high_star = [(110, 110.5, 99.5, 100), (105.5, 106.2, 104.8, 105.8), (105.8, 107.5, 105.5, 107)]
    assert "morning_star" not in found(*high_star)


# ------------------------------------------------------------ граничные пороги


def test_doji_threshold_inclusive():
    assert "doji" in found((100, 105, 95, 101))           # ровно 10%
    assert "doji" not in found((100, 105, 95, 101.01))    # 10.1%


def test_threshold_tolerates_float_rounding():
    """0.31-0.30 = 0.010000000000000009 > 0.1 * 0.0999...98: без допуска граница терялась бы."""
    assert "doji" in found((0.30, 0.35, 0.25, 0.31))


def test_doji_threshold_is_a_parameter():
    assert "doji" not in found((100, 102, 98, 100.5))
    assert "doji" in found((100, 102, 98, 100.5), cfg=CandleConfig(doji_body_max=0.2))


def test_pin_bar_tail_and_nose_boundaries():
    assert "pin_bar_bull" in found((100, 101.2, 98, 101))        # хвост ровно 2 тела
    assert "pin_bar_bull" not in found((100, 101.2, 98.01, 101))
    assert "pin_bar_bull" in found((100, 102, 97, 101))          # нос ровно 20%
    assert "pin_bar_bull" not in found((100, 102.01, 97, 101))
    assert "pin_bar_bull" not in found((100, 101.5, 97, 101), cfg=CandleConfig(pin_tail_body_mult=3.5))


def test_harami_half_body_boundary():
    mother = (104, 104.5, 95.5, 96)
    assert "harami_bull" in found(mother, (98, 102.5, 97.5, 102))         # ровно 50%
    assert "harami_bull" not in found(mother, (98, 102.5, 97.5, 102.01))


def test_marubozu_boundary():
    assert "marubozu_bull" in found((100, 110, 100, 109))      # ровно 90%
    assert "marubozu_bull" not in found((100, 110, 99.9, 109))


def test_star_boundaries():
    a, m = (110, 110.5, 99.5, 100), (100, 101, 98.5, 100.5)
    assert "morning_star" in found(a, m, (100.5, 106.5, 100.2, 105))                 # ровно середина
    assert "morning_star" in found((110, 115, 95, 100), m, (100.5, 106.5, 100.2, 106))  # тело первой 50%
    assert "morning_star" not in found((110, 115.1, 95, 100), m, (100.5, 106.5, 100.2, 106))
    assert "morning_star" in found(a, (100, 104, 99.5, 103), (103, 106.5, 102.8, 106.5))  # звезда 30%
    assert "morning_star" not in found(a, (100, 104, 99.5, 103.01), (103.01, 106.6, 102.8, 106.6))


# ------------------------------------------------------------ сверка с pandas-ta


def _random_ohlc(n: int, seed: int, half_range) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    mid = 100 + np.cumsum(rng.normal(0, 0.5, n))
    half = half_range(rng, n)
    low, high = mid - half, mid + half
    return pd.DataFrame({
        "ts": T0 + np.arange(n) * M15, "open": rng.uniform(low, high), "high": high,
        "low": low, "close": rng.uniform(low, high), "volume": 1000.0,
    })


def test_doji_matches_pandas_ta_on_equal_ranges():
    """pandas-ta 0.4.71b0 cdl_doji: тело < 10% СРЕДНЕГО диапазона 10 свечей. При равных
    диапазонах определения совпадают (граница на случайных ценах — мера нуль)."""
    import pandas_ta as ta

    df = _random_ohlc(400, 1, lambda rng, n: np.full(n, 1.0))
    lib = (ta.cdl_doji(df.open, df.high, df.low, df.close) != 0).to_numpy()
    mine = np.array(["doji" in detect_patterns(df, i) for i in range(len(df))])
    assert mine[9:].sum() >= 10
    assert (mine[9:] == lib[9:]).all()


def test_inside_bar_matches_pandas_ta():
    import pandas_ta as ta

    df = _random_ohlc(400, 2, lambda rng, n: rng.uniform(0.2, 2.0, n))
    lib = (ta.cdl_inside(df.open, df.high, df.low, df.close) != 0).to_numpy()
    mine = np.array(["inside_bar" in detect_patterns(df, i) for i in range(len(df))])
    assert mine[1:].sum() >= 10
    assert (mine[1:] == lib[1:]).all()  # [0] у pandas-ta сравнивается с последней (np.roll)


# ------------------------------------------------------------------- контекст


def trend(n: int, step: float) -> list[tuple]:
    """Ровный тренд: close шагает на step, open = прошлый close, тени по 0.5 -> TR = 2."""
    rows, prev = [], 100.0
    for t in range(1, n + 1):
        c = 100.0 + step * t
        rows.append((prev, max(prev, c) + 0.5, min(prev, c) - 0.5, c))
        prev = c
    return rows


def test_context_downtrend_at_low():
    assert pattern_context(bars(*trend(30, -1.0))) == ("low", -5.0)  # -10 за 10 свечей / ATR 2


def test_context_uptrend_at_high():
    assert pattern_context(bars(*trend(30, 1.0))) == ("high", 5.0)


def test_context_both_and_none():
    flat = [(100, 100.5, 99.5, 100)] * 24
    assert pattern_context(bars(*flat, (100, 105, 95, 100.2)))[0] == "both"
    spike = flat[:10] + [(100, 110, 90, 100)] + flat[:14]  # экстремум окна 11 свечей назад
    assert pattern_context(bars(*spike))[0] is None


def test_context_needs_history():
    assert pattern_context(bars(*trend(19, -1.0)))[0] is None   # окно 20 свечей
    assert pattern_context(bars(*trend(15, -1.0)))[1] is None   # ATR(14) до свечи + 10 баров хода
    assert pattern_context(bars(*trend(16, -1.0)))[1] == -5.0


def test_atr_scale_excludes_the_labelled_bar():
    base = trend(30, -1.0)
    giant = base[:-1] + [(base[-1][0], 150.0, 50.0, base[-1][3])]
    assert pattern_context(bars(*giant))[1] == pattern_context(bars(*base))[1]


# ---------------------------------------------------------------- look-ahead


def _varied_df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.6, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, 0.4, n))
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, 0.4, n))
    return pd.DataFrame({"ts": T0 + np.arange(n) * M15, "open": open_, "high": hi,
                         "low": lo, "close": close, "volume": 1000.0})


def test_no_look_ahead_prefix_equals_full_series():
    """Разметка свечи i по префиксу (как вживую: i — последняя закрытая) == разметка той же
    свечи в полной серии: будущие свечи не участвуют."""
    df = _varied_df()
    seen = set()
    for k in range(1, len(df) + 1):
        live = label_frame(df.iloc[:k].reset_index(drop=True))
        assert live == label_frame(df, k - 1)
        seen.update(live.patterns)
    assert len(seen) >= 6  # проверка не пустая: паттерны реально встречаются


def test_future_bars_do_not_change_labels():
    df = _varied_df()
    for k in (25, 120, 299):
        moved = df.copy()
        moved.loc[k:, ["open", "high", "low", "close"]] *= 1.37
        assert detect_patterns(moved, k - 1) == detect_patterns(df, k - 1)
        assert pattern_context(moved, k - 1) == pattern_context(df, k - 1)


def test_shadow_labels_last_closed_bar_of_each_tf():
    frames = {
        "15m": _varied_df(130, 1),
        "1h": _varied_df(260, 2).assign(ts=lambda d: T0 + np.arange(len(d)) * 3_600_000),
        "4h": _varied_df(260, 3).assign(ts=lambda d: T0 + np.arange(len(d)) * 14_400_000),
    }
    sh = shadow_labels(frames)
    assert sh.detector == CANDLES_VERSION
    assert list(sh.frames) == ["15m", "1h", "4h"]  # сигнальный первым
    for tf, df in frames.items():
        assert sh.frames[tf].bar_ts == int(df["ts"].iloc[-1])
        assert sh.frames[tf] == label_frame(df)


# ------------------------------------------------------------------ контракт


def full_shadow() -> CandleShadow:
    """Все паттерны на всех ТФ у экстремума — худший случай для утечки в гейт."""
    frame = CandleFrame(bar_ts=T0, patterns=list(PATTERNS), at_extreme="low", prior_move_atr=-3.0)
    return CandleShadow(detector=CANDLES_VERSION, frames={"15m": frame, "1h": frame, "4h": frame})


def with_shadow(c: SetupCandidate, shadow: CandleShadow | None = None) -> SetupCandidate:
    f = c.features.model_copy(update={"candle_patterns_shadow": shadow or full_shadow()})
    return SetupCandidate.model_validate(c.model_copy(update={"features": f}).model_dump())


def test_contract_fixed_menu_and_roundtrip():
    with pytest.raises(ValidationError):
        CandleFrame(bar_ts=T0, patterns=["hammer"])  # имя вне меню схемы
    with pytest.raises(ValidationError):
        CandleShadow(detector="x", frames={"2h": CandleFrame(bar_ts=T0)})
    c = with_shadow(ladder_candidate())
    assert c.schema_version == 2
    assert SetupCandidate.model_validate_json(c.model_dump_json()) == c


def test_prompt_exclude_drops_only_the_shadow():
    c = with_shadow(ladder_candidate())
    full = json.loads(c.model_dump_json())
    slim = json.loads(c.model_dump_json(exclude=PROMPT_EXCLUDE))
    assert full["features"].pop("candle_patterns_shadow")["detector"] == CANDLES_VERSION
    assert full == slim
    assert set(slim["features"]) == V1_FEATURE_KEYS


# ---------------------------------------- инвариант ст. 5: решения те же (бит-в-бит)


def _verdict_json(inp) -> str:
    return evaluate(inp, LADDER_CAPS, LADDER_PROFILE).model_dump_json(exclude={"ts"})


def _ladder_inputs():
    only_ob = features_ok("short").model_copy(update={"fvg": None})  # 1 сигнал из 3
    none_ = features_ok("short").model_copy(update={"fvg": None, "ob": None})
    counter = features_ok("short").model_copy(update={"trend_mtf": "bullish"})
    return [
        good_input(),                                                        # проходит
        good_input(candidate=ladder_candidate(features=only_ob)),            # confluence 1/3
        good_input(candidate=ladder_candidate(features=none_)),              # confluence 0/3
        good_input(candidate=ladder_candidate(features=counter)),            # против тренда
        good_input(candidate=ladder_candidate(direction="long", features=features_ok("long")),
                   sl=98.5, tp=104.0),                                       # long проходит
        good_input(tp=98.0),                                                 # R:R
        good_input(confidence=59),                                           # ранний стоп
    ]


@pytest.mark.parametrize("idx", range(7))
def test_ladder_verdict_identical_with_shadow(idx):
    from dataclasses import replace

    inp = _ladder_inputs()[idx]
    j = replace(inp, candidate=with_shadow(inp.candidate))
    assert j.candidate.features.candle_pattern is None
    assert _verdict_json(j) == _verdict_json(inp)


def test_shadow_is_not_a_confluence_vote_but_candle_pattern_would_be():
    """Контроль чувствительности: тот же сетап с 1 сигналом проходит, если паттерн
    положить в candle_pattern, — значит, равенство выше не случайно: тень не голосует."""
    from dataclasses import replace

    inp = _ladder_inputs()[1]
    shadowed = evaluate(replace(inp, candidate=with_shadow(inp.candidate)), LADDER_CAPS, LADDER_PROFILE)
    assert not shadowed.passed and shadowed.steps[-1].value.startswith("signals=1/3")
    voted = inp.candidate.features.model_copy(update={"candle_pattern": "doji"})
    leaked = replace(inp, candidate=inp.candidate.model_copy(update={"features": voted}))
    assert evaluate(leaked, LADDER_CAPS, LADDER_PROFILE).passed


def test_agent_prompts_identical_with_shadow():
    plain = agent_candidate()
    j = with_shadow(plain)
    for p_plain, p_j in (
        (scout_mod.build_prompt(plain, "", ""), scout_mod.build_prompt(j, "", "")),
        (trader_mod.build_prompt(plain, scout_verdict()), trader_mod.build_prompt(j, scout_verdict())),
    ):
        assert p_j == p_plain
        assert "candle_patterns_shadow" not in p_j and '"candle_pattern": null' in p_j
        slim = j.model_dump_json(indent=2, exclude=PROMPT_EXCLUDE)
        assert slim in p_j and set(json.loads(slim)["features"]) == V1_FEATURE_KEYS


# ------------------------------------------------------------------- цикл


class TfCache:
    """Свечи по ТФ + учет запросов (блок J не должен добавлять ни одного)."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.calls: list[tuple[str, int]] = []

    def get(self, symbol, timeframe, limit):
        self.calls.append((timeframe, limit))
        return self.frames[timeframe].copy()


def _run(tmp_path, name, df, escalator=None):
    state = RuntimeState(journal=connect(tmp_path / f"{name}.db"))
    cfg = CycleConfig(signal_timeframe="15m", context_timeframes=("1h", "4h"),
                      min_score=30, smc=SmcConfig(swing_length=5))
    cache = TfCache({"15m": df, "1h": df, "4h": df})
    report = ShadowCycle(cache, state, CORE_CAPS, CORE_PROFILE, cfg,
                         escalator=escalator).run_once([SYM], 1000.0)
    return state, report, cache


def _strip(candidate_json: str) -> dict:
    c = json.loads(candidate_json)
    c.pop("ts")
    c["features"].pop("candle_patterns_shadow")
    return c


def _journal(state) -> list:
    dec = [
        (r["decision_id"], _strip(r["candidate_json"]), r["scout_json"], r["trade_json"],
         {k: v for k, v in json.loads(r["risk_json"]).items() if k != "ts"},
         r["outcome"], r["outcome_detail"])
        for r in state.journal.execute("SELECT * FROM decisions ORDER BY decision_id")
    ]
    cf = [
        (r["decision_id"], r["cohort"], _strip(r["candidate_json"]),
         r["entry_price"], r["sl_price"], r["tp_price"])
        for r in state.journal.execute("SELECT * FROM counterfactuals ORDER BY decision_id")
    ]
    return [dec, cf]


def _no_ts(prompt: str) -> str:
    return re.sub(r'"ts": "[^"]+"', '"ts": "<now>"', prompt)  # ts кандидата — время создания


def _recording_escalator(prompts: list):
    def esc(c):
        prompts.append((_no_ts(scout_mod.build_prompt(c, "", "")),
                        _no_ts(trader_mod.build_prompt(c, scout_verdict()))))
        return EscalationResult(outcome="scout_pass", detail="stub")
    return esc


def test_cycle_behaviour_identical_without_block_j(tmp_path, monkeypatch):
    """Ст. 5: тот же рынок с тенью и без нее -> те же отчеты, решения, когорты, цены
    counterfactual, вердикты лестницы, промпты эскалации и запросы свечей. Отличие в
    журнале — только само поле тени."""
    full = market_df(160, seed=3)
    runs = {}
    for name in ("j", "no_j"):
        if name == "no_j":
            monkeypatch.setattr(cycle_mod, "shadow_or_none", lambda frames: None)
        out = []
        for n in (100, 112, 124, 136, 148, 160):  # 124 проходит лестницу -> эскалация
            prompts: list = []
            df = full.iloc[:n].reset_index(drop=True)
            state, report, cache = _run(tmp_path, f"{name}{n}", df, _recording_escalator(prompts))
            rows = state.journal.execute("SELECT candidate_json FROM decisions").fetchall()
            shadows = [json.loads(r["candidate_json"])["features"] for r in rows]
            out.append((report, _journal(state), prompts, cache.calls, shadows))
        runs[name] = out

    for (rep_j, jr_j, pr_j, calls_j, sh_j), (rep_n, jr_n, pr_n, calls_n, sh_n) in zip(
        runs["j"], runs["no_j"], strict=True
    ):
        assert rep_j == rep_n
        assert jr_j == jr_n
        assert pr_j == pr_n
        assert calls_j == calls_n == [("15m", 300), ("1h", 260), ("4h", 260)]
        assert all(f["candle_pattern"] is None for f in sh_j + sh_n)
        assert all(f["candle_patterns_shadow"] is None for f in sh_n)
        for f in sh_j:
            assert list(f["candle_patterns_shadow"]["frames"]) == ["15m", "1h", "4h"]
    reports = [r[0] for r in runs["j"]]
    assert any(r.risk_passed for r in reports) and any(r.candidates > r.risk_passed for r in reports)
    assert sum(len(r[2]) for r in runs["j"]) >= 1  # эскалация была и промпт сверен


def test_cycle_shadow_marks_signal_bar(tmp_path):
    df = market_df(160, seed=3).iloc[:124].reset_index(drop=True)
    state, report, _ = _run(tmp_path, "bar", df)
    assert report.candidates == 1
    row = state.journal.execute("SELECT decision_id, candidate_json FROM decisions").fetchone()
    shadow = json.loads(row["candidate_json"])["features"]["candle_patterns_shadow"]
    bar_ts = int(df["ts"].iloc[-1])
    assert shadow["frames"]["15m"]["bar_ts"] == bar_ts and f"-{bar_ts}-" in row["decision_id"]
    assert shadow["frames"]["15m"] == label_frame(df).model_dump()


def test_detector_failure_never_touches_the_cycle(tmp_path, monkeypatch, caplog):
    df = market_df(160, seed=3).iloc[:124].reset_index(drop=True)
    base_state, base_report, _ = _run(tmp_path, "base", df)
    base = _journal(base_state)

    def boom(frames, cfg=DEFAULT_CONFIG):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(candles, "shadow_labels", boom)
    with caplog.at_level(logging.WARNING, logger="oskar.candles"):
        state, report, _ = _run(tmp_path, "broken", df)
    assert report == base_report and not report.errors
    assert _journal(state) == base
    row = state.journal.execute("SELECT candidate_json FROM decisions").fetchone()
    assert json.loads(row["candidate_json"])["features"]["candle_patterns_shadow"] is None
    assert "candle shadow failed" in caplog.text
    assert shadow_or_none({"15m": df}) is None
