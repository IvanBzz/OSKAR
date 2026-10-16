"""Комбинации меню entry×sl×tp через риск-лестницу (блок F, 2026-10-16).

Меню — декартово произведение опций, а лестницу проходят немногие: Trader видел
все и выбирал заведомо непроходные (7 из 14 «execute» срезала финальная лестница).
Один перебор на всех: гейт цикла (best_passing_combo) и список допустимых
комбинаций для Trader'а. Числа — только из детерминированного меню (ст. 2).
"""

from dataclasses import dataclass

from oskar.contracts.schemas import PriceOption, RiskVerdict, SetupCandidate
from oskar.detect.options import combo_allowed
from oskar.risk.ladder import Caps, OpenPosition, ProfileLimits, RiskInput, evaluate


@dataclass(frozen=True)
class ComboEval:
    """Одна комбинация меню и ее вердикт лестницы."""

    entry: PriceOption
    sl: PriceOption
    tp: PriceOption
    verdict: RiskVerdict

    @property
    def passed(self) -> bool:
        return self.verdict.passed

    @property
    def ids(self) -> tuple[str, str, str]:
        return self.entry.id, self.sl.id, self.tp.id

    @property
    def label(self) -> str:
        return "/".join(self.ids)

    @property
    def prices(self) -> tuple[float, float, float]:
        return self.entry.price, self.sl.price, self.tp.price

    @property
    def entry_type(self) -> str:
        return "market" if self.entry.kind == "market" else "limit"

    @property
    def rr(self) -> float | None:
        """R:R после комиссий (rr_fee_adjusted) — только у проходной."""
        return self.verdict.computed.rr_fee_adjusted if self.verdict.computed else None

    @property
    def sl_pct(self) -> float:
        return abs(self.entry.price - self.sl.price) / self.entry.price * 100.0

    @property
    def steps_passed(self) -> int:
        return sum(s.passed for s in self.verdict.steps)

    @property
    def failed_step(self) -> str | None:
        return next((s.name for s in self.verdict.steps if not s.passed), None)


def evaluate_combos(
    candidate: SetupCandidate,
    caps: Caps,
    profile: ProfileLimits,
    *,
    confidence: int,
    equity_usd: float,
    open_positions: list[OpenPosition] | None = None,
    safe_mode: bool = False,
    paused: bool = False,
) -> list[ComboEval]:
    """Все осмысленные комбинации в порядке меню (entry -> sl -> tp): детерминированно,
    реплей воспроизводит. Бессмысленные сочетания (R-тейк чужой пары вход/стоп, ATR-стоп
    чужого входа) отсекает options.combo_allowed — до лестницы."""
    positions = list(open_positions or [])
    out = []
    for e in candidate.entry_options:
        for s in candidate.sl_options:
            for t in candidate.tp_options:
                if not combo_allowed(candidate, e, s, t):
                    continue
                v = evaluate(
                    RiskInput(candidate=candidate, entry=e.price, sl=s.price, tp=t.price,
                              confidence=confidence, equity_usd=equity_usd,
                              open_positions=positions, safe_mode=safe_mode, paused=paused),
                    caps, profile,
                )
                out.append(ComboEval(entry=e, sl=s, tp=t, verdict=v))
    return out


def _choice_key(c: ComboEval) -> tuple[float, int, float]:
    # структурный тейк (пул ликвидности, FVG) раньше R-кратного: туда цену тянет
    return c.rr, int(c.tp.kind == "r_multiple"), c.sl_pct


def rank_combos(evals: list[ComboEval]) -> list[ComboEval]:
    """Проходные комбинации в порядке единого правила выбора (2026-10-16, аудит F).

    Минимальный rr_fee_adjusted, то есть ближайшая цель, еще удовлетворяющая капу
    R:R; при равенстве — структурный тейк раньше R-кратного, затем меньшая
    дистанция стопа, затем порядок меню. Зачем: максимум R:R почти всегда брал
    t-r1 или дальний тейк (медиана прошедших 11.5R, 54/93 выше 10R, максимум
    107.6R) — counterfactual мерил нереалистичные цели. Одно правило на гейт
    цикла, fallback эскалатора и порядок списка в промпте Trader'а."""
    return sorted((c for c in evals if c.passed), key=_choice_key)  # sorted стабилен


def choose_combo(evals: list[ComboEval]) -> ComboEval | None:
    """Комбинация по единому правилу (см. rank_combos); None — проходных нет."""
    ranked = rank_combos(evals)
    return ranked[0] if ranked else None


def most_advanced(evals: list[ComboEval]) -> ComboEval | None:
    """Комбинация, прошедшая больше всего шагов лестницы; при равенстве — первая.

    Зачем (2026-10-16): вердикт дефолтной комбинации обрывался на R:R и журнал
    писал «R:R», хотя почти всё резал confluence_gate шагом дальше."""
    best = None
    for c in evals:
        if best is None or c.steps_passed > best.steps_passed:
            best = c
    return best
