"""Shadow-поток демона: сканирование рынка и накопление counterfactual-датасета.

Ритм (все интервалы — инжектируемые, тесты управляют временем):
- каждые REFRESH_TOP_SEC (30 мин): universe -> quick_filter -> top-N вотчлист
- каждые tick_seconds (интервал профиля): ShadowCycle.run_once по вотчлисту
- каждые RESOLVE_SEC (5 мин): дорешивание counterfactual-исходов
- каждые EQUITY_SEC (1 час): обновление equity с биржи (fallback: прежнее значение)

Устойчивость (правило v1): серия из consecutive_errors подряд неудачных тиков
-> safe_mode + запись в лог. Один упавший символ цикл не валит (это внутри
ShadowCycle), тут ловятся падения инфраструктуры (сеть, биржа).
"""

import logging
import threading
import time

from oskar.core.cycle import ShadowCycle
from oskar.core.state import RuntimeState
from oskar.data.scanner import quick_filter

logger = logging.getLogger("oskar.shadow")

REFRESH_TOP_SEC = 1800.0
RESOLVE_SEC = 300.0
EQUITY_SEC = 3600.0
DEFAULT_EQUITY_USD = 1000.0


class ShadowRunner:
    def __init__(
        self,
        cycle: ShadowCycle,
        cache,
        client,
        state: RuntimeState,
        *,
        top_n: int,
        min_quote_volume: float,
        tick_seconds: float,
        consecutive_errors_to_safe_mode: int = 3,
        equity_fn=None,  # () -> float | None; None = без биржи (paper equity)
        clock=time.monotonic,
    ):
        self._cycle = cycle
        self._cache = cache
        self._client = client
        self._state = state
        self._top_n = top_n
        self._min_qv = min_quote_volume
        self._tick_sec = tick_seconds
        self._err_limit = consecutive_errors_to_safe_mode
        self._equity_fn = equity_fn
        self._clock = clock

        self.watchlist: list[str] = []
        self.equity_usd: float = DEFAULT_EQUITY_USD
        self.ticks: int = 0
        self.last_report: dict = {}
        self._err_streak = 0
        self._top_at = -REFRESH_TOP_SEC
        self._resolve_at = 0.0
        self._equity_at = -EQUITY_SEC
        self._stop = threading.Event()

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        """Один такт. Все падения инфраструктуры ловятся здесь."""
        now = self._clock()
        try:
            if now - self._equity_at >= EQUITY_SEC and self._equity_fn is not None:
                eq = self._equity_fn()
                if eq is not None and eq > 0:
                    self.equity_usd = eq
                self._equity_at = now

            if now - self._top_at >= REFRESH_TOP_SEC:
                universe = self._client.universe()
                top = quick_filter(
                    self._cache, universe, top_n=self._top_n,
                    min_quote_volume=self._min_qv,
                )
                self.watchlist = [q.symbol for q in top]
                self._top_at = now
                logger.info("watchlist refreshed: %d symbols", len(self.watchlist))

            report = self._cycle.run_once(self.watchlist, self.equity_usd)
            self.ticks += 1
            self.last_report = {
                "scanned": report.scanned, "scored": report.scored,
                "candidates": report.candidates, "risk_passed": report.risk_passed,
                "errors": len(report.errors),
            }
            if report.candidates:
                logger.info("tick %d: %s", self.ticks, self.last_report)
            for e in report.errors:
                logger.warning("cycle error: %s", e)

            if now - self._resolve_at >= RESOLVE_SEC:
                for symbol in set(self.watchlist):
                    try:
                        self._cycle.resolve_counterfactuals(symbol)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("resolve %s: %s", symbol, e)
                self._resolve_at = now

            self._err_streak = 0
        except Exception as e:  # noqa: BLE001 - инфраструктура (сеть/биржа)
            self._err_streak += 1
            logger.error("shadow tick failed (%d/%d): %s",
                         self._err_streak, self._err_limit, e)
            if self._err_streak >= self._err_limit:
                self._state.safe_mode = True
                logger.error("SAFE MODE: %d consecutive shadow failures", self._err_streak)

    # ---------------------------------------------------------------- thread

    def run_forever(self) -> None:
        logger.info("shadow runner started (tick=%.0fs, top=%d)", self._tick_sec, self._top_n)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._tick_sec)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-shadow", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
