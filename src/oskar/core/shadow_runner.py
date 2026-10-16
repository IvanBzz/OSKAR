"""Shadow-поток демона: сканирование рынка и накопление counterfactual-датасета.

Ритм (все интервалы — инжектируемые, тесты управляют временем):
- каждые REFRESH_TOP_SEC (30 мин): universe -> scan -> top-N вотчлист
- каждые tick_seconds (интервал профиля): ShadowCycle.run_once по вотчлисту
- каждые RESOLVE_SEC (5 мин): дорешивание counterfactual-исходов — по символам
  с висящими исходами, не по вотчлисту (выпавший из топа символ иначе висел вечно)
- каждые EQUITY_SEC (1 час): обновление equity с биржи; после сбоя — повтор через
  EQUITY_RETRY_SEC, а не через час (исполнение заблокировано, пока equity не с биржи)
- каждые HEARTBEAT_SEC (5 мин): строка-пульс «heartbeat: tick N, ...» в лог — ее ищет
  сторож супервизора (scripts/supervise.ps1) в data/oskard.log

Устойчивость (правило v1): серия из consecutive_errors подряд неудачных тиков
-> safe_mode + уведомление. Считаются сбои ЛОГИКИ и тики, где упали ВСЕ символы;
сетевой чих (network/transient/cooldown по classify_error) в серию не идет —
минутный обрыв на границе пересборки вотчлиста уводил в вечный safe_mode.

Cooldown kline-эндпоинта (бан 109429 / бюджет ошибок клиента): цикл и
пересборка вотчлиста ждут окончания паузы — одна строка в лог, не шторм
ошибок по 20 символам каждый тик. Прерванный скан старый вотчлист не трогает:
частичный топ перекошен к началу алфавита.

Защелка просадки / panic (блок I, 2026-10-16): пока стоит защелка drawdown_halt (блок H) или
panic, цикл (run_once) не запускается — иначе кандидаты уходили к Scout/Trader, бюджет LLM
тратился, а исполнитель все равно отказывал. Строка в лог — раз в HALT_LOG_SEC; дорешивание
counterfactuals и equity идут как обычно. SAFE MODE и пауза цикл останавливают сами.
"""

import html
import logging
import threading
import time

from oskar.core.cycle import ShadowCycle
from oskar.core.emergency import engage_safe_mode, panic_engaged, retry_safe_mode_persist
from oskar.core.state import RuntimeState
from oskar.data.client import classify_error
from oskar.data.scanner import scan
from oskar.risk.drawdown import read_halt

logger = logging.getLogger("oskar.shadow")

REFRESH_TOP_SEC = 1800.0
REFRESH_RETRY_SEC = 120.0  # пересборка упала по сети — повтор через 2 мин, не через 30
RESOLVE_SEC = 300.0
EQUITY_SEC = 3600.0
EQUITY_RETRY_SEC = 300.0
DEFAULT_EQUITY_USD = 1000.0
MAX_RESOLVE_SYMBOLS = 40  # за один проход: висящие исходы дорешиваются порциями
NET_STREAK_FACTOR = 4  # сетевые сбои: серия в 4 раза длиннее (3 -> 12 тиков = 6 мин при 30с)
# Пульс потока в лог. Строка тика пишется только при кандидатах: на тихом рынке лог молчал
# по 25 мин (15.10) — сторож супервизора («лог не обновлялся 15 мин») срабатывал бы ложно.
# Формат строки — контракт с scripts/supervise.ps1 ($HeartbeatPattern), см. тест.
HEARTBEAT_SEC = 300.0
HALT_LOG_SEC = 1800.0  # «цикл стоит: защелка просадки/panic» — не чаще раза в 30 мин


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
        notify=None,  # (text) -> None: алерт в Telegram при safe_mode
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
        self._notify = notify
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
        self._net_streak = 0
        self._top_at = -REFRESH_TOP_SEC
        self._resolve_at = 0.0
        self._equity_at = -EQUITY_SEC
        self._equity_interval = EQUITY_SEC
        self._heartbeat_at: float | None = None  # None — первый пульс сразу после старта потока
        self._candidates_at: float | None = None  # последний тик с кандидатами (часы self._clock)
        self._halt_logged_at: float | None = None  # последняя строка «цикл стоит» (None — не стоит)
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

    def _refresh_equity(self) -> None:
        eq = self._equity_fn()
        if eq is not None and eq > 0:
            self.equity_usd = eq
            self.equity_source = "exchange"
            self._persist_equity(eq)
            self._equity_interval = EQUITY_SEC
            return
        if self.equity_source == "exchange":
            self.equity_source = "persisted"  # биржа молчит: значение устарело
        self._equity_interval = EQUITY_RETRY_SEC
        logger.warning(
            "equity fetch FAILED - using %s value %.2f (execution blocked until exchange answers; "
            "retry in %.0fs)", self.equity_source, self.equity_usd, EQUITY_RETRY_SEC,
        )

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

    def _resolve_symbols(self) -> list[str]:
        fn = getattr(self._cycle, "unresolved_symbols", None)
        if callable(fn):
            try:
                return list(fn())[:MAX_RESOLVE_SYMBOLS]
            except Exception as e:  # noqa: BLE001 - журнал: падаем на вотчлист
                logger.warning("unresolved_symbols failed: %s", e)
        return sorted(set(self.watchlist))

    # ------------------------------------------------------------------ tick

    def _fail(self, e: Exception, what: str) -> None:
        """Сбой инфраструктуры. Логика/все символы — серия из err_limit; сеть/биржа
        временно — отдельная, в NET_STREAK_FACTOR раз длиннее серия: минутный чих
        не приговор, но получасовой обрыв — тоже safe_mode."""
        kind = classify_error(e)
        if kind in ("network", "transient", "cooldown"):
            self._net_streak += 1
            limit = self._err_limit * NET_STREAK_FACTOR
            logger.warning("%s failed (%s, %d/%d): %s", what, kind, self._net_streak, limit, e)
            if self._net_streak >= limit:
                self._enter_safe_mode(f"{self._net_streak} consecutive network failures ({what}: {e})")
            return
        self._err_streak += 1
        logger.error("%s failed (%d/%d): %s", what, self._err_streak, self._err_limit, e)
        if self._err_streak >= self._err_limit:
            self._enter_safe_mode(f"{self._err_streak} consecutive shadow failures ({what}: {e})")

    def _enter_safe_mode(self, why: str) -> None:
        if self._state.safe_mode:
            return
        engage_safe_mode(self._state, why)  # флаг + meta: переживает рестарт (блок H, 2026-10-16)
        msg = f"SAFE MODE: {why}"
        logger.error(msg)
        if self._notify is not None:
            try:  # send_text шлет parse_mode=HTML, а в why — текст исключения
                self._notify("🛑 " + html.escape(msg, quote=False))
            except Exception as ne:  # noqa: BLE001
                logger.warning("notify failed: %s", ne)

    def _entries_halted(self) -> str | None:
        """panic или защелка просадки — в памяти и в meta журнала (переживают рестарт)."""
        st = self._state
        try:
            if st.panic_at is not None or panic_engaged(st.journal):
                return "panic"
            halt = read_halt(st.journal)
        except Exception as e:  # noqa: BLE001 - meta не прочитана: цикл решит сам (он тоже читает журнал)
            logger.warning("entry halt flags unreadable: %s", e)
            return None
        return None if halt is None else f"стоп по просадке −{halt.pct:.1f}% от пика ${halt.peak:.2f}"

    def _skip_cycle(self, now: float, why: str) -> None:
        """Тик без цикла: поток жив (тик считается, пульс идет), к Scout/Trader никто не уходит."""
        self.ticks += 1
        self.last_report = {"halted": why}
        if self._halt_logged_at is None or now - self._halt_logged_at >= HALT_LOG_SEC:
            self._halt_logged_at = now
            logger.warning("cycle skipped: %s - no escalation to Scout/Trader until it is lifted", why)

    def tick(self) -> None:
        """Один такт. Все падения инфраструктуры ловятся здесь."""
        retry_safe_mode_persist(self._state)  # SAFE MODE не записан в meta — повтор (ревью Codex R1)
        now = self._clock()
        if now - self._equity_at >= self._equity_interval and self._equity_fn is not None:
            try:
                self._refresh_equity()
            except Exception as e:  # noqa: BLE001 - equity_fn сам глотает сеть; это журнал
                logger.warning("equity refresh failed: %s", e)
            self._equity_at = now

        cooldown = self._cooldown_remaining()
        if now - self._top_at >= REFRESH_TOP_SEC:
            if cooldown > 0:
                logger.warning("watchlist refresh postponed: kline API cooldown %.0fs", cooldown)
                self._top_at = now - REFRESH_TOP_SEC + cooldown + 1.0
            else:
                try:
                    self._refresh_watchlist(now)
                except Exception as e:  # noqa: BLE001
                    self._fail(e, "watchlist refresh")
                    self._top_at = now - REFRESH_TOP_SEC + REFRESH_RETRY_SEC
                    return
                cooldown = self._cooldown_remaining()  # скан мог нарваться на бан

        if cooldown > 0:
            if not self._cooldown_announced:
                logger.warning(
                    "cycle skipped: kline API cooldown %.0fs (%s)",
                    cooldown, getattr(self._client, "cooldown_reason", ""),
                )
                self._cooldown_announced = True
            return
        self._cooldown_announced = False

        halt = self._entries_halted()
        if halt is not None:
            self._skip_cycle(now, halt)
        else:
            if self._halt_logged_at is not None:
                logger.warning("cycle resumed: entry halt lifted")
                self._halt_logged_at = None
            try:
                report = self._cycle.run_once(self.watchlist, self.equity_usd)
            except Exception as e:  # noqa: BLE001 - инфраструктура (сеть/биржа/логика)
                self._fail(e, "cycle")
                return
            self.ticks += 1
            self.last_report = {
                "scanned": report.scanned, "scored": report.scored,
                "candidates": report.candidates, "risk_passed": report.risk_passed,
                "errors": len(report.errors),
            }
            if report.candidates:
                self._candidates_at = now
                logger.info("tick %d: %s", self.ticks, self.last_report)
            for e in report.errors:
                if e not in self._seen_cycle_errors:
                    self._seen_cycle_errors.add(e)
                    logger.warning("cycle error (далее молчу до смены вотчлиста): %s", e)
            if report.scanned and len(report.errors) >= report.scanned:
                self._fail(RuntimeError(f"all {report.scanned} symbols failed"), "cycle")
                return

        if now - self._resolve_at >= RESOLVE_SEC:
            for symbol in self._resolve_symbols():
                try:
                    self._cycle.resolve_counterfactuals(symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning("resolve %s: %s", symbol, e)
            self._resolve_at = now

        self._err_streak = 0
        self._net_streak = 0

    def heartbeat(self) -> bool:
        """Пульс раз в HEARTBEAT_SEC независимо от кандидатов, cooldown и сбоев цикла:
        он говорит «поток жив и крутится», а не «рынок интересный». Зовется из цикла
        потока ПОСЛЕ tick(): застрявший тик = нет пульса = алерт «демон завис» у
        супервизора. True — строка записана."""
        now = self._clock()
        if self._heartbeat_at is not None and now - self._heartbeat_at < HEARTBEAT_SEC:
            return False
        self._heartbeat_at = now
        if self._candidates_at is None:
            last = "нет с запуска"
        else:
            last = f"{(now - self._candidates_at) / 60:.0f} мин назад"
        logger.info("heartbeat: tick %d, last candidates %s", self.ticks, last)
        return True

    # ---------------------------------------------------------------- thread

    def run_forever(self) -> None:
        logger.info("shadow runner started (tick=%.0fs, top=%d)", self._tick_sec, self._top_n)
        while not self._stop.is_set():
            self.tick()
            self.heartbeat()
            self._stop.wait(self._tick_sec)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-shadow", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
