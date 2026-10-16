"""Сайзинг и леверидж — вычисляет ТОЛЬКО Python (CONSTITUTION.md ст. 2 п. 3).

Формулы портированы из v1 (vault: dynamic-leverage.md, Phase 1 stop-bleeding):
- fee-aware R:R: из TP-дистанции вычитается 2x round-trip fee ДО расчета
- динамический леверидж = max_lev * conf_factor * vol_factor, вниз до целого
  (v1: «потери на плохих сделках уменьшились в ~5 раз»)
- риск-базированный размер: qty = risk_usd / stop_loss_per_unit, где потеря на
  единицу при стоп-ауте = ход до SL + проскальзывание stop-market + taker-комиссии
  входа и выхода. Было qty = risk_usd / sl_distance: кап держал только ход цены, и
  на стопе терялся risk_pct ПЛЮС издержки (ревью 2026-10-13, Codex #21).

Комиссия определена один раз (TAKER_FEE_PCT за сторону -> ROUND_TRIP_FEE_PCT за
круг): R:R и сайзинг считают от одного числа. Funding не учитывается — знак
переменный, порядка 0.01% notional за 8ч.
"""

import math
from dataclasses import dataclass

# Комиссия BingX USDT-M perpetual, taker, ЗА СТОРОНУ, % от notional ноги: markets
# info takerFeeRate 0.0005 (ccxt bingx: fees.swap.taker). Обе ноги считаем taker —
# маркет-вход и STOP_MARKET-выход таковы; лимитный вход мог бы пройти maker (0.02%),
# но риск-кап считается по худшему случаю.
TAKER_FEE_PCT = 0.05
# Круг = вход + выход, % от entry. Ревью 2026-10-13: было 0.08 «за круг» и еще x2 в
# R:R. 0.08 занижало реальный круг taker (2 x 0.05 = 0.10), а x2 в R:R — не «две
# ноги», а запас 2:1 из v1 fix 1.3 (см. RR_FEE_SAFETY_MULT).
ROUND_TRIP_FEE_PCT = 2 * TAKER_FEE_PCT
# TP обязан покрыть круг ДВАЖДЫ (v1 fix 1.3: «комиссии с 2:1 запасом»). На этом
# определении стоит min_rr_fee_adjusted в hash-pinned caps.yaml («ПОСЛЕ вычета 2x
# round-trip fee») — уменьшить множитель = ослабить кап в обход ст. 4.
RR_FEE_SAFETY_MULT = 2
# Проскальзывание стоп-аута, % от entry. SL на BingX — STOP_MARKET: по триггеру уходит
# рыночный ордер и исполняется хуже триггера на спред и задержку срабатывания.
# 5 bps — несколько тиков на ликвидных перпах вотчлиста (пол объема сканера $200k/ч)
# при наших размерах в доли стакана. Гэп или каскад ликвидаций статический буфер не
# покроет: этот хвост держит max_total_risk_pct, а саму константу калибровать по
# exit_price фактических sl_hit в журнале.
STOP_SLIPPAGE_PCT = 0.05
RR_TOLERANCE = 0.01        # урок v1: 1.49985 отклонялся против floor 1.5
# qty * L при qty = risk / L может уйти на 1 ulp выше risk: шум float, не превышение
# капа. Денежный риск сравнивается с капсом с этим относительным допуском.
RISK_CAP_REL_EPS = 1e-9
# Шум float в произведении факторов: 5 * conf(80) * vol(2.7%) = 1.9999999999999996
# вместо 2 — floor без допуска молча срезал бы плечо на целую ступень.
_LEVERAGE_FLOAT_EPS = 1e-9


def fee_adjusted_rr(entry: float, sl: float, tp: float) -> float:
    """R:R после вычета комиссий. Может быть отрицательным, если TP не покрывает fee.

    Из TP-дистанции вычитается RR_FEE_SAFETY_MULT кругов от entry (2 x 0.10% =
    0.20%) — «2x round-trip fee» из caps.yaml. Знаменатель — ценовая дистанция до SL:
    в этих единицах задан min_rr_fee_adjusted. Деньги на стопе с издержками держит
    сайзинг (stop_loss_per_unit), а не R:R.
    """
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist <= 0:
        raise ValueError("SL distance must be positive")
    return (tp_dist - rr_fee_buffer(entry)) / sl_dist


def rr_fee_buffer(entry: float) -> float:
    """Ценовой буфер комиссий, который R:R вычитает из TP-дистанции (2 круга от entry)."""
    return RR_FEE_SAFETY_MULT * (ROUND_TRIP_FEE_PCT / 100.0) * entry


def tp_for_fee_adjusted_rr(entry: float, sl: float, rr: float) -> float:
    """Обратная к fee_adjusted_rr: TP, при котором R:R после комиссий равен rr.

    2026-10-16 (блок F): R-тейки меню считались валовыми — «2.0R» после вычета
    буфера давал < 2.0 и не проходил min_rr_fee_adjusted никогда (мертвая опция).
    Сторона — по стопу: SL ниже entry = long, тейк выше."""
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        raise ValueError("SL distance must be positive")
    sign = 1.0 if sl < entry else -1.0
    return entry + sign * (rr * sl_dist + rr_fee_buffer(entry))


def confidence_factor(confidence: int) -> float:
    """Линейно 0.3 -> 1.0 на confidence 50 -> 100; ниже 50 = 0.3."""
    if confidence <= 50:
        return 0.3
    return min(1.0, 0.3 + (confidence - 50) / 50.0 * 0.7)


def volatility_factor(sl_dist_pct: float) -> float:
    """1.5% SL-дистанции = норма; шире стоп -> меньше леверидж. Кап 1.0.

    Восстановлено по примерам v1: 60%/3.0% -> 2.2x, 90%/0.5% -> 8.6x (при max 10).
    """
    if sl_dist_pct <= 0:
        raise ValueError("sl_dist_pct must be positive")
    return min(1.0, 1.5 / sl_dist_pct)


def raw_leverage(max_leverage: float, confidence: int, sl_dist_pct: float) -> float:
    """Формула v1 без округления (воркед-примеры vault: 60%/3.0% -> 2.2x,
    90%/0.5% -> 8.6x при max 10). На биржу идет только dynamic_leverage()."""
    return max_leverage * confidence_factor(confidence) * volatility_factor(sl_dist_pct)


def dynamic_leverage(max_leverage: float, confidence: int, sl_dist_pct: float) -> int:
    """Целое плечо: формула v1 вниз до целого, минимум 1x, не выше max_leverage.

    BingX принимает только целое плечо, адаптер при отправке округляет вниз. Раньше
    здесь было round(..., 1) -> 4.4x: лестница проверяла маржу на 4.4x, а на биржу
    уходило 4x (ревью 2026-10-13, Codex #20). Теперь проверяется ровно отправляемое.
    floor, а не round: меньше плечо = больше маржа, и кап не превышается по
    построению. При max_leverage < 1 вернется 1x > капа — это отклонит шаг «leverage».
    """
    raw = raw_leverage(max_leverage, confidence, sl_dist_pct)
    return max(1, math.floor(min(raw + _LEVERAGE_FLOAT_EPS, max_leverage)))


def stop_exit_price(entry: float, sl: float) -> float:
    """Расчетная цена исполнения стопа: триггер SL, сдвинутый проскальзыванием ПРОТИВ
    позиции (long: стоп ниже входа -> еще ниже; short: выше -> еще выше)."""
    slip = STOP_SLIPPAGE_PCT / 100.0 * entry
    return sl - slip if sl < entry else sl + slip


def stop_loss_per_unit(entry: float, sl: float) -> float:
    """Потеря на 1 единицу qty при стоп-ауте, С издержками, в USDT.

    |entry - exit| + taker * (entry + exit), exit = stop_exit_price(): ход цены до
    исполнения стопа с проскальзыванием плюс комиссии обеих ног, каждая от своей цены
    (биржа берет комиссию с notional ноги). Оценка «круг от entry» недобирала бы на
    short: там выход дороже входа.
    """
    if entry <= 0 or sl <= 0:
        raise ValueError("entry and SL must be positive")
    if abs(entry - sl) <= 0:
        raise ValueError("SL distance must be positive")
    exit_px = stop_exit_price(entry, sl)
    fees = TAKER_FEE_PCT / 100.0 * (entry + exit_px)
    return abs(entry - exit_px) + fees


def qty_for_risk(risk_usd: float, entry: float, sl: float) -> float:
    """Наибольшая qty, чья потеря при стоп-ауте С издержками = risk_usd.

    Единственная формула «риск -> qty»: ей же обязан пользоваться пересайзинг под
    фактическую цену входа, иначе издержки снова выпадут из капа."""
    return risk_usd / stop_loss_per_unit(entry, sl)


@dataclass
class PositionSize:
    """risk_usd — потеря при стоп-ауте С ИЗДЕРЖКАМИ: qty * stop_loss_per_unit() (ход
    до стопа + проскальзывание stop-market + taker-комиссии входа и выхода).
    leverage — целое, ровно то, что уйдет на биржу; margin_usd = notional / leverage."""

    qty: float
    leverage: int
    risk_usd: float
    margin_usd: float
    notional_usd: float


def position_size(
    equity_usd: float,
    risk_pct: float,
    entry: float,
    sl: float,
    leverage: int,
) -> PositionSize:
    """Риск-базированный размер: потеря при стоп-ауте С издержками = risk_pct от equity."""
    if equity_usd <= 0:
        raise ValueError("equity must be positive")
    if leverage < 1 or leverage != int(leverage):
        # дробное плечо биржа не примет: маржа на нем — проверка не того, что уйдет
        raise ValueError(f"leverage must be a whole number >= 1, got {leverage}")
    budget_usd = equity_usd * risk_pct / 100.0
    qty = qty_for_risk(budget_usd, entry, sl)
    notional = qty * entry
    margin = notional / leverage
    return PositionSize(
        qty=qty, leverage=int(leverage), risk_usd=qty * stop_loss_per_unit(entry, sl),
        margin_usd=margin, notional_usd=notional,
    )
