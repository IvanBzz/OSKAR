"""Двухступенчатый сканер (v1-паттерн: ~600 пар -> quick filter 1h -> top-20).

Ступень 1 (quick): дешевый скоринг по 1h-индикаторам (объем, RSI-экстремум,
определенность тренда, подвижность ATR) — отбирает кандидатов для дорогой
ступени 2 (полный SMC-анализ + score.py в цикле демона).

Ошибка по одному символу (мало истории, делистинг) — символ пропускается,
скан продолжается (graceful degradation, принцип v1). Исключение одно:
ExchangeCooldown — kline-эндпоинт на паузе (бан 109429 / бюджет ошибок),
дальнейший перебор бессмыслен — скан останавливается, а ScanSummary честно
говорит, что топ частичный. Ошибки больше не глотаются молча: сводка по
классам уходит в лог (урок 2026-10-11: 109415 месяц не всплывали в логе).
"""

from collections import Counter
from dataclasses import dataclass, field

from oskar.data.candles import drop_on_data_error
from oskar.data.client import ExchangeCooldown
from oskar.detect.indicators import compute, tf_trend

QUICK_TIMEFRAME = "1h"
QUICK_CANDLES = 80
# Пол ликвидности: средний ЧАСОВОЙ оборот в quote (volume*close по 1h-барам).
# Живая проба 2026-08-26: без пола топ-20 забивают мемкоины с нулевым оборотом
# (RSI-экстремум неликвида = дутые очки, спреды убийственные).
MIN_HOURLY_QUOTE_VOLUME_USD = 200_000.0
MAX_ERROR_SAMPLES = 3


@dataclass(frozen=True)
class QuickScore:
    symbol: str
    score: int
    volume_ratio: float
    rsi: float
    trend: str
    atr_pct: float


@dataclass
class ScanSummary:
    """Что случилось со сканом: сколько отскорено, отрезано по ликвидности,
    упало (по классам) и остановлен ли перебор досрочно."""

    total: int = 0
    scored: int = 0
    illiquid: int = 0
    errors: Counter = field(default_factory=Counter)  # класс ошибки -> счетчик
    samples: list[str] = field(default_factory=list)  # первые тексты ошибок (для лога)
    aborted: str | None = None  # причина досрочной остановки (cooldown)

    def note_error(self, symbol: str, e: Exception) -> None:
        self.errors[type(e).__name__] += 1
        if len(self.samples) < MAX_ERROR_SAMPLES:
            self.samples.append(f"{symbol}: {type(e).__name__}: {str(e)[:120]}")

    def as_log(self) -> str:
        err_total = sum(self.errors.values())
        parts = [
            f"universe {self.total}", f"scored {self.scored}",
            f"illiquid {self.illiquid}", f"errors {err_total}",
        ]
        if self.errors:
            parts[-1] += " (" + ", ".join(f"{k} {v}" for k, v in self.errors.most_common()) + ")"
        if self.samples:
            parts.append("e.g. " + " | ".join(self.samples))
        return "; ".join(parts)


def quick_score_one(
    cache,
    symbol: str,
    min_quote_volume: float = MIN_HOURLY_QUOTE_VOLUME_USD,
    summary: ScanSummary | None = None,
) -> QuickScore | None:
    """None = символ не скорится (неликвид/мало данных/ошибка) — пропуск.

    ExchangeCooldown пробрасывается: это пауза всего эндпоинта, не символа.
    """
    try:
        df = cache.get(symbol, QUICK_TIMEFRAME, QUICK_CANDLES)
        avg_quote_vol = float((df["volume"] * df["close"]).tail(20).mean())
    except ExchangeCooldown:
        raise
    except Exception as e:  # noqa: BLE001 - любой сбой одного символа не валит скан
        if summary is not None:
            summary.note_error(symbol, e)
        return None
    if avg_quote_vol < min_quote_volume:
        if summary is not None:
            summary.illiquid += 1
        return None
    try:
        # битое окно — запись кеша долой (1h общий с циклом); мало истории — остается
        with drop_on_data_error(cache, symbol, QUICK_TIMEFRAME):
            ind = compute(df)
    except Exception as e:  # noqa: BLE001 - мало истории и т.п.
        if summary is not None:
            summary.note_error(symbol, e)
        return None

    score = 0
    if ind.volume_ratio >= 2.0:
        score += 40
    elif ind.volume_ratio >= 1.5:
        score += 30

    if ind.rsi <= 30 or ind.rsi >= 70:
        score += 20
    elif ind.rsi <= 35 or ind.rsi >= 65:
        score += 10

    trend = tf_trend(ind)
    if trend != "mixed":
        score += 20

    if ind.atr_pct >= 0.5:
        score += 10

    return QuickScore(
        symbol=symbol, score=score, volume_ratio=round(ind.volume_ratio, 2),
        rsi=round(ind.rsi, 1), trend=trend, atr_pct=round(ind.atr_pct, 3),
    )


def scan(
    cache,
    symbols: list[str],
    top_n: int = 20,
    min_quote_volume: float = MIN_HOURLY_QUOTE_VOLUME_USD,
) -> tuple[list[QuickScore], ScanSummary]:
    """Скорит символы, возвращает (top_n по убыванию, сводка).

    На ExchangeCooldown перебор останавливается: топ частичный, summary.aborted
    заполнен — решение, что с этим делать, за вызывающим (runner держит старый
    вотчлист).
    """
    summary = ScanSummary(total=len(symbols))
    scored: list[QuickScore] = []
    for s in symbols:
        try:
            qs = quick_score_one(cache, s, min_quote_volume, summary)
        except ExchangeCooldown as e:
            summary.aborted = str(e)
            break
        if qs is not None:
            scored.append(qs)
    summary.scored = len(scored)
    scored.sort(key=lambda q: (-q.score, q.symbol))
    return scored[:top_n], summary


def quick_filter(
    cache,
    symbols: list[str],
    top_n: int = 20,
    min_quote_volume: float = MIN_HOURLY_QUOTE_VOLUME_USD,
) -> list[QuickScore]:
    """Скорит все символы, возвращает top_n по убыванию (при равенстве — по имени)."""
    return scan(cache, symbols, top_n=top_n, min_quote_volume=min_quote_volume)[0]
