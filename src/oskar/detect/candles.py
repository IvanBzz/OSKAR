"""Свечные паттерны по OHLC — ТЕНЕВОЙ детектор (блок J, 2026-10-16).

Детерминированные формулы без TA-Lib. Разметка пишется в
SetupFeatures.candle_patterns_shadow (журнал decisions/counterfactuals) и в решения
НЕ идет: confluence-гейт лестницы смотрит только candle_pattern (детектор его не
заполняет), промпты Scout/Trader это поле исключают (contracts.PROMPT_EXCLUDE).
Зачем тень: замер 16.10 (vault/drafts) — доджи у 20-барного экстремума без контекста
не лучше обычной и случайной свечи, литература после издержек тоже почти пуста.
Сначала когорты «с паттерном / без» на живых кандидатах, решение о гейте — потом,
отдельным коммитом с инвариантом (ст. 5).

Look-ahead: свеча i размечается только по свечам <= i (срез df.iloc[:i+1] до любых
расчетов). На вход — закрытые свечи (CandleCache, формирующуюся отбросил клиент),
по умолчанию размечается последняя.

Пороги — доли диапазона/тела САМОЙ свечи, а не среднего за 10 баров, как в TA-Lib:
формулы локальны и проверяемы на эталонных свечах. Границы включительны (с допуском
на округление float). Свеча без диапазона (high == low) формы не имеет — паттерном
не считается. Для крипты 24/7 звезды — без требования гэпа между телами.

С pandas-ta НЕ эквивалентно (ревью J, 2026-10-16, тесты фиксируют расхождения):
cdl_doji сравнивает тело со средним диапазоном 10 свечей и строго (<) — совпадает с
нашим доджи только при равных диапазонах; cdl_inside строг с обеих сторон — на равной
high или low у нас внутренний бар, у pandas-ta нет.
"""

import logging
import math
from dataclasses import dataclass
from typing import get_args

import pandas as pd

from oskar.contracts.schemas import CandleFrame, CandlePattern, CandleShadow

logger = logging.getLogger("oskar.candles")

CANDLES_VERSION = "candles-1"
PATTERNS: tuple[str, ...] = get_args(CandlePattern)  # порядок вывода = порядок меню схемы
_EPS = 1e-9  # допуск на округление float при сравнении долей


@dataclass(frozen=True)
class CandleConfig:
    doji_body_max: float = 0.10       # доджи: тело <= 10% диапазона
    doji_shadow_max: float = 0.10     # стрекоза/надгробие: верхняя/нижняя тень <= 10% диапазона
    pin_tail_body_mult: float = 2.0   # пин-бар: хвост >= 2 тел
    pin_nose_max: float = 0.20        # пин-бар: противоположная тень <= 20% диапазона
    harami_body_max: float = 0.5      # харами: тело второй <= 50% тела первой
    star_first_body_min: float = 0.5  # звезда: тело первой >= 50% ее диапазона
    star_body_max: float = 0.3        # звезда: тело средней <= 30% тела первой
    star_close_into: float = 0.5      # звезда: третья закрывается за серединой тела первой
    marubozu_body_min: float = 0.9    # марубозу: тело >= 90% диапазона
    extreme_window: int = 20          # экстремум — минимум/максимум последних 20 свечей,
    extreme_span: int = 3             # которого касаются последние 3 (самый длинный паттерн)
    move_bars: int = 10               # движение к свече: close[i-1] - close[i-1-10]
    atr_bars: int = 14                # ATR — среднее TR за 14 свечей до размечаемой


DEFAULT_CONFIG = CandleConfig()


@dataclass(frozen=True)
class _Bar:
    open: float
    high: float
    low: float
    close: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def top(self) -> float:  # верх тела
        return max(self.open, self.close)

    @property
    def bottom(self) -> float:  # низ тела
        return min(self.open, self.close)

    @property
    def upper(self) -> float:
        return self.high - self.top

    @property
    def lower(self) -> float:
        return self.bottom - self.low

    @property
    def bull(self) -> bool:
        return self.close > self.open

    @property
    def bear(self) -> bool:
        return self.close < self.open

    @property
    def mid(self) -> float:  # середина тела
        return (self.open + self.close) / 2


def _bar(df: pd.DataFrame, i: int) -> _Bar:
    row = df.iloc[i]
    return _Bar(float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))


def _le(x: float, limit: float, scale: float) -> bool:
    """x <= limit с допуском на округление float в масштабе scale."""
    return x <= limit + _EPS * scale


def _single(b: _Bar, cfg: CandleConfig) -> set[str]:
    """Одна свеча: форма по долям ее диапазона."""
    if not b.range > 0:
        return set()
    r = b.range
    out: set[str] = set()
    if _le(b.body, cfg.doji_body_max * r, r):
        out.add("doji")
        if _le(b.upper, cfg.doji_shadow_max * r, r):
            out.add("dragonfly_doji")
        if _le(b.lower, cfg.doji_shadow_max * r, r):
            out.add("gravestone_doji")
    # пин-бар по форме, без цвета: молот/повешенный (хвост вниз) и перевернутый
    # молот/падающая звезда (хвост вверх). Контекст — в at_extreme/prior_move_atr.
    if _le(cfg.pin_tail_body_mult * b.body, b.lower, r) and _le(b.upper, cfg.pin_nose_max * r, r):
        out.add("pin_bar_bull")
    if _le(cfg.pin_tail_body_mult * b.body, b.upper, r) and _le(b.lower, cfg.pin_nose_max * r, r):
        out.add("pin_bar_bear")
    if _le(cfg.marubozu_body_min * r, b.body, r):
        out.add("marubozu_bull" if b.bull else "marubozu_bear")
    return out


def _double(p: _Bar, b: _Bar, cfg: CandleConfig) -> set[str]:
    """Две свечи: p — предыдущая, b — размечаемая."""
    out: set[str] = set()
    # поглощение по телам: противоположный цвет, тело b накрывает тело p и больше его
    if p.bear and b.bull and b.open <= p.close and b.close >= p.open and b.body > p.body:
        out.add("engulfing_bull")
    if p.bull and b.bear and b.open >= p.close and b.close <= p.open and b.body > p.body:
        out.add("engulfing_bear")
    # внутренний бар: диапазон b внутри диапазона p, границы включительно (price action:
    # high <= и low >= предыдущей), хотя бы одна строго; совпадение обеих — не внутри.
    # НАМЕРЕННО шире pandas-ta cdl_inside (строго с обеих сторон): на ценах с шагом тика
    # равная граница частая — у нас inside, у pandas-ta нет (ревью J, 2026-10-16, тест)
    if (p.range > 0 and b.high <= p.high and b.low >= p.low
            and (b.high < p.high or b.low > p.low)):
        out.add("inside_bar")
    # харами: тело b внутри тела p и не больше половины; направление — по цвету p (как TA-Lib)
    if (p.body > 0 and b.top <= p.top and b.bottom >= p.bottom
            and _le(b.body, cfg.harami_body_max * p.body, p.body)):
        out.add("harami_bull" if p.bear else "harami_bear")
    return out


def _triple(a: _Bar, m: _Bar, b: _Bar, cfg: CandleConfig) -> set[str]:
    """Утренняя/вечерняя звезда: длинная a, маленькая звезда m за серединой тела a
    (вместо гэпа — для крипты 24/7), b противоположного цвета закрывается за серединой a."""
    out: set[str] = set()
    if not a.range > 0 or not a.body > 0:
        return out
    long_first = _le(cfg.star_first_body_min * a.range, a.body, a.range)
    small_star = _le(m.body, cfg.star_body_max * a.body, a.body)
    if not (long_first and small_star and b.body > m.body):
        return out
    reach = cfg.star_close_into * a.body
    if a.bear and m.top <= a.mid and b.bull and _le(a.close + reach, b.close, a.body):
        out.add("morning_star")
    if a.bull and m.bottom >= a.mid and b.bear and _le(b.close, a.close - reach, a.body):
        out.add("evening_star")
    return out


def _index(df: pd.DataFrame, i: int | None) -> int:
    n = len(df)
    if n == 0:
        raise ValueError("no candles")
    k = n - 1 if i is None else (i + n if i < 0 else i)
    if not 0 <= k < n:
        raise IndexError(f"bar {i} out of range for {n} candles")
    return k


def detect_patterns(
    df: pd.DataFrame, i: int | None = None, cfg: CandleConfig = DEFAULT_CONFIG
) -> list[str]:
    """Паттерны, ЗАВЕРШИВШИЕСЯ на свече i (по умолчанию последняя = последняя закрытая).

    Видит только свечи <= i. Порядок — как в меню схемы (PATTERNS)."""
    k = _index(df, i)
    past = df.iloc[: k + 1]  # look-ahead невозможен по построению
    found = _single(_bar(past, k), cfg)
    if k >= 1:
        found |= _double(_bar(past, k - 1), _bar(past, k), cfg)
    if k >= 2:
        found |= _triple(_bar(past, k - 2), _bar(past, k - 1), _bar(past, k), cfg)
    return [p for p in PATTERNS if p in found]


def pattern_context(
    df: pd.DataFrame, i: int | None = None, cfg: CandleConfig = DEFAULT_CONFIG
) -> tuple[str | None, float | None]:
    """(at_extreme, prior_move_atr) свечи i по свечам <= i.

    at_extreme: последние extreme_span свечей касаются минимума ("low") / максимума
    ("high") / обоих ("both") окна extreme_window свечей; None — нет или истории мало.
    prior_move_atr: close[i-1] - close[i-1-move_bars] в ATR(atr_bars), посчитанном по
    свечам до i (сама свеча масштаб не сжимает); None — истории мало или ATR = 0."""
    k = _index(df, i)
    past = df.iloc[: k + 1]
    high = past["high"].to_numpy(dtype=float)
    low = past["low"].to_numpy(dtype=float)
    close = past["close"].to_numpy(dtype=float)

    at_extreme = None
    if k + 1 >= cfg.extreme_window:
        win = slice(k + 1 - cfg.extreme_window, k + 1)
        span = slice(k + 1 - cfg.extreme_span, k + 1)
        at_low = bool(low[span].min() <= low[win].min())
        at_high = bool(high[span].max() >= high[win].max())
        if at_low and at_high:
            at_extreme = "both"  # широкая свеча накрыла все окно
        elif at_low or at_high:
            at_extreme = "low" if at_low else "high"

    prior_move = None
    j = k - 1  # движение ДО размечаемой свечи
    if j - cfg.move_bars >= 0 and j - cfg.atr_bars >= 0:
        tr = [
            max(high[t] - low[t], abs(high[t] - close[t - 1]), abs(low[t] - close[t - 1]))
            for t in range(j - cfg.atr_bars + 1, j + 1)
        ]
        atr = sum(tr) / len(tr)
        if atr > 0:
            move = (close[j] - close[j - cfg.move_bars]) / atr
            prior_move = round(float(move), 3) if math.isfinite(move) else None
    return at_extreme, prior_move


def label_frame(
    df: pd.DataFrame, i: int | None = None, cfg: CandleConfig = DEFAULT_CONFIG
) -> CandleFrame:
    """Разметка свечи i одного ТФ: паттерны + контекст + ts открытия свечи."""
    k = _index(df, i)
    at_extreme, prior_move = pattern_context(df, k, cfg)
    return CandleFrame(
        bar_ts=int(df["ts"].iloc[k]),
        patterns=detect_patterns(df, k, cfg),
        at_extreme=at_extreme,
        prior_move_atr=prior_move,
    )


def shadow_labels(
    frames: dict[str, pd.DataFrame], cfg: CandleConfig = DEFAULT_CONFIG
) -> CandleShadow:
    """Тень по последней закрытой свече каждого ТФ (порядок frames сохраняется:
    сигнальный первым). Свечи — уже загруженные циклом, новых запросов нет."""
    return CandleShadow(
        detector=CANDLES_VERSION,
        frames={tf: label_frame(df, cfg=cfg) for tf, df in frames.items() if len(df)},
    )


def shadow_or_none(
    frames: dict[str, pd.DataFrame], cfg: CandleConfig = DEFAULT_CONFIG
) -> CandleShadow | None:
    """shadow_labels, но сбой детектора -> None и warning: тень не имеет права менять
    поведение цикла, в том числе исключением (символ не должен выпасть из-за нее)."""
    try:
        return shadow_labels(frames, cfg)
    except Exception as e:  # noqa: BLE001 - тень никогда не валит цикл
        logger.warning("candle shadow failed: %s: %s", type(e).__name__, e)
        return None
