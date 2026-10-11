"""Shadow-поток демона: сканирование рынка и накопление counterfactual-датасета.

Ритм (все интервалы — инжектируемые, тесты управляют временем):
- каждые REFRESH_TOP_SEC (30 мин): universe -> scan -> top-N вотчлист
- каждые tick_seconds (интервал профиля): ShadowCycle.run_once по вотчлисту
- каждые RESOLVE_SEC (5 мин): дорешивание counterfactual-исходов
- каждые EQUITY_SEC (1 час): обновление equity с биржи (fallback: прежнее значение)

Устойчивость (правило v1): серия из consecutive_errors подряд неудачных тиков
-> safe_mode + запись в лог. Один упавший символ цикл не валит (это внутри
ShadowCycle), тут ловятся падения инфраструктуры (сеть, биржа).

Cooldown kline-эндпоинта (бан 109429 / бюджет ошибок клиента): цикл и
пересборка вотчлиста ждут окончания паузы — одна строка в лог, не шторм
ошибок по 20 символам каждый тик. Прерванный скан старый вотчлист не трогает:
частичный топ перекошен к началу алфавита.
"""

import logging
import threading
import time

from oskar.core.cycle import ShadowCycle
from oskar.core.state import RuntimeState
from oskar.data.scanner import scan

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
        # Откуда equity: exchange | persisted (последнее известное с биржи,
        # пережило рестарт) | default. Исполнение разрешено ТОЛЬКО при exchange
        # (ст. 1: биржа — единственная правда; сайзинг на выдуманном equity —
        # класс бага «журнал врет»). Ключ протух -> демон это видит, а не маскирует.
        self.equity_source: str = "default"
        self.equity_at: str | None = None
        self._load_persisted_equity()
        self.ticks: int = 0
        self.last_report: dict = {}
        self._seen_cycle_errors: set[str] = set()  # дедуп шума (AGT-кейс: одна и та же ошибка каждый тик)
        self._cooldown_announced = False
        self._err_streak = 0
        self._top_at = -REFRESH_TOP_SEC
        self._resolve_at = 0.0
        self._equity_at = -EQUITY_SEC
        self._stop = threading.Event()

    # ---------------------------------------------------------------- equity

    def _load_persisted_equity(self) -> None:
        try:
            row = self._state.journal.execute(
                "SELECT key, value FROM meta WHERE key IN ('equity_last','equity_last_at')"
            ).fetchall()
            kv = {r[0]: r[1] for r in row}
            if "equity_last" in kv:
                self.equity_usd = float(kv["equity_last"])
                self.equity_at = kv.get("equity_last_at")
                self.equity_source = "persisted"
        except Exception as e:  # noqa: BLE001 - журнал без meta: остаемся на default
            logger.warning("persisted equity unavailable: %s", e)

    def _persist_equity(self, value: float) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        j = self._state.journal
        j.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('equity_last', ?)", (str(value),))
        j.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('equity_last_at', ?)", (now,))
        j.commit()
        self.equity_at = now

    # ------------------------------------------------------------ data health

    def _cooldown_remaining(self) -> float:
        fn = getattr(self._client, "cooldown_remaining", None)
        return float(fn()) if callable(fn) else 0.0

    def data_health(self) -> dict:
        """Срез клиента данных для /status (пустой, если клиент его не ведет)."""
        fn = getattr(self._client, "health", None)
        return dict(fn()) if callable(fn) else {}

    def _refresh_watchlist(self, now: float) -> None:
        universe = self._client.universe()
        top, summary = scan(
            self._cache, universe, top_n=self._top_n, min_quote_volume=self._min_qv,
        )
        fresh = [q.symbol for q in top]
        if summary.aborted:
            logger.warning("watchlist scan ABORTED (%s): %s", summary.aborted, summary.as_log())
            if not self.watchlist and fresh:
                self.watchlist = fresh  # лучше перекошенный список, чем простой с пустым
                logger.warning("watchlist from PARTIAL scan: %d symbols (no previous list)", len(fresh))
            # повтор сразу после паузы, а не через 30 минут
            self._top_at = now - REFRESH_TOP_SEC + self._cooldown_remaining() + 1.0
            return
        self.watchlist = fresh
        self._top_at = now
        self._seen_cycle_errors.clear()  # новый вотчлист — ошибки можно повторить
        logger.info("watchlist refreshed: %d symbols | %s", len(self.watchlist), summary.as_log())

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        """Один такт. Все падения инфраструктуры ловятся здесь."""
        now = self._clock()
        try:
            if now - self._equity_at >= EQUITY_SEC and self._equity_fn is not None:
                eq = self._equity_fn()
                if eq is not None and eq > 0:
                    self.equity_usd = eq
                    self.equity_source = "exchange"
                    self._persist_equity(eq)
                else:
                    if self.equity_source == "exchange":
                        self.equity_source = "persisted"  # биржа молчит: значение устарело
                    logger.warning(
                        "equity fetch FAILED - using %s value %.2f "
                        "(execution blocked until exchange answers)",
                        self.equity_source, self.equity_usd,
                    )
                self._equity_at = now

            cooldown = self._cooldown_remaining()
            if now - self._top_at >= REFRESH_TOP_SEC:
                if cooldown > 0:
                    logger.warning("watchlist refresh postponed: kline API cooldown %.0fs", cooldown)
                    self._top_at = now - REFRESH_TOP_SEC + cooldown + 1.0
                else:
                    self._refresh_watchlist(now)
                    cooldown = self._cooldown_remaining()  # скан мог нарваться на бан

            if cooldown > 0:
                if not self._cooldown_announced:
                    logger.warning(
                        "cycle skipped: kline API cooldown %.0fs (%s)",
                        cooldown, getattr(self._client, "cooldown_reason", ""),
                    )
                    self._cooldown_announced = True
            else:
                self._cooldown_announced = False
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
                    if e not in self._seen_cycle_errors:
                        self._seen_cycle_errors.add(e)
                        logger.warning("cycle error (далее молчу до смены вотчлиста): %s", e)

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
