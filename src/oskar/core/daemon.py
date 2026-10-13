"""oskard — точка входа демона.

Порядок старта (каждый шаг обязателен, отказ = не стартуем):
1. Проверка капсов против конституции (ст. 4) — mismatch = отказ запуска
2. Single-instance lock: пробный бинд 127.0.0.1:8321 — занят = второй
   экземпляр, громкая ошибка (v1: два экземпляра слили ADA в двойную позицию)
3. Журнал + RuntimeState (run_id)
4. uvicorn с управляющим API

Торговый цикл (ShadowCycle) в Фазе 1 запускается отдельно/вручную;
фоновый поток цикла подключается вместе с исполнением в Фазе 2.
"""

import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321


class SingleInstanceError(RuntimeError):
    pass


def check_port_free(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Пробный бинд. Занято = уже работает другой oskard."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        raise SingleInstanceError(
            f"port {host}:{port} is taken - another oskard instance is running. "
            "NEVER run two instances on one account (v1 lesson: doubled ADA position)."
        ) from e
    finally:
        s.close()


def acquire_instance_lock(path: Path):
    """Файловый замок на ВСЁ время жизни процесса (пробный бинд порта оставлял
    окно гонки: сокет закрыт, потоки стартуют, uvicorn биндится позже).
    Возвращает открытый дескриптор — держать до выхода."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")  # noqa: SIM115 - живет до конца процесса
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise SingleInstanceError(
            f"lock {path} is held - another oskard instance is running. "
            "NEVER run two instances on one account (v1 lesson: doubled ADA position)."
        ) from e
    return fh


def live_trades_in(db_path: Path) -> int:
    """Сколько open/pending сделок в журнале на диске (read-only; 0, если таблицы нет)."""
    import sqlite3

    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status IN ('open','pending')"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _connect_journal():
    """Журнал; при несовпадении версии схемы — архив старого и чистый старт.
    Допустимо ТОЛЬКО без живых сделок: с open/pending в базе отказываемся
    стартовать (иначе теряются order id и управление позициями). None = отказ."""
    from oskar.journal.archive import archive_journal
    from oskar.journal.db import connect

    db_path = REPO_ROOT / "data" / "oskar.db"
    try:
        return connect(db_path)
    except RuntimeError as e:
        live = live_trades_in(db_path)
        if live:
            print(f"REFUSING TO START: journal schema mismatch ({e}) with {live} live trade(s) "
                  "- migrate the journal, do not wipe it", flush=True)
            return None
        snap = archive_journal(db_path)
        for suffix in ("", "-wal", "-shm"):
            p = db_path.parent / (db_path.name + suffix)
            p.unlink(missing_ok=True)
        print(f"journal schema mismatch ({e}); old journal archived to {snap}", flush=True)
        return connect(db_path)


def _make_escalator(state, caps, profile_settings, settings, runner, cache):
    """Escalator строится, только если claude CLI и Telegram доступны."""
    import os
    import shutil

    if shutil.which("claude") is None:
        print("escalation OFF: claude CLI not found (pure shadow mode)")
        return None

    from oskar.agents.budget import BudgetLimits
    from oskar.agents.scout import analyze_candidate, knowledge_for_candidate
    from oskar.agents.trader import decide, resolve_prices
    from oskar.core.escalation import Escalator

    a = settings.get("agents", {})
    scout_limits = BudgetLimits(
        daily=int(a.get("scout", {}).get("daily_budget", 30)),
        hourly=int(a.get("scout", {}).get("hourly_budget", 6)),
    )
    trader_limits = BudgetLimits(daily=int(a.get("trader", {}).get("daily_budget", 20)))

    def scout_fn(candidate):
        return analyze_candidate(
            candidate, state.journal, state.run_id, limits=scout_limits,
            knowledge=knowledge_for_candidate(candidate),
        ).verdict

    def trader_fn(candidate, scout_verdict):
        return decide(
            candidate, scout_verdict, state.journal, state.run_id,
            limits=trader_limits,
        ).decision

    card_sink = None
    # Карточки шлет СИГНАЛЬНЫЙ бот (кнопки ловит наш поллер); фоллбек на
    # основного бота оставлен, но его кнопки мертвы (Hermes глотает чужие
    # callback'и) — лог предупредит.
    token = os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN"):
        print("WARNING: TELEGRAM_SIGNALS_BOT_TOKEN not set - card buttons will be dead")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat:
        from oskar.core.telegram_out import TelegramSender, render_chart

        sender = TelegramSender(token, chat)
        ttl_min = profile_settings.approval_ttl_seconds // 60

        def card_sink(candidate, verdict, scout_verdict, plan=None):  # noqa: F811
            png = None
            try:
                df = cache.get(candidate.symbol, candidate.timeframe, 200)
                png = render_chart(df, candidate, plan)
            except Exception:  # noqa: BLE001 - карточка без графика лучше отсутствия
                pass
            sent = sender.send_setup_card(candidate, verdict, png, ttl_min, scout_verdict, plan)
            if not sent.ok:
                return False  # эскалатор увидит отказ Telegram, а не промолчит
            # message_id -> pending_approvals: монитор снимет кнопки по истечении TTL
            return sent.message_id
    else:
        print("escalation cards OFF: telegram not configured")

    from oskar.core.bootstrap import load_caps
    from oskar.core.escalation import open_positions_from_journal

    return Escalator(
        state, load_caps(), profile_settings.limits,
        scout_fn=scout_fn, trader_fn=trader_fn, resolve_fn=resolve_prices,
        approval_ttl_seconds=profile_settings.approval_ttl_seconds,
        card_sink=card_sink,
        # портфель для лестницы: без него max_open_positions/total_risk/дубли
        # не срабатывали никогда (ревью 2026-10-13)
        open_positions_fn=lambda: open_positions_from_journal(state.journal),
        equity_fn=lambda: runner.equity_usd,
    )


def _sandbox(settings) -> bool:
    return bool(settings.get("exchange", {}).get("sandbox", True))


def _make_executor(state, settings, caps, client):
    """Исполнитель по кнопке. Среда (VST/реал) — из settings.exchange.sandbox,
    та же, что у equity и монитора. Цена для проверки «план не устарел» — с
    публичного тикера; лимит позиций — из капсов."""
    import os

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        return None
    from oskar.core.executor import ExecutionService
    from oskar.data.client import make_bingx_exchange
    from oskar.execution.bingx import BingXAdapter

    adapter = BingXAdapter(make_bingx_exchange(key, secret, sandbox=_sandbox(settings)))
    try:
        adapter._ensure_markets()  # первое нажатие не должно ждать load_markets
    except Exception as e:  # noqa: BLE001 - подгрузится при первом ордере
        print(f"WARNING: markets preload failed: {e}", flush=True)
    return ExecutionService(
        adapter, state.journal, state.run_id,
        price_fn=client.last_price, max_open_positions=caps.max_open_positions,
    )


def _make_monitor(state, settings, profile, token: str, chat_id: str):
    """Монитор позиций: свой ccxt-экземпляр (не делим сессию с кнопками),
    уведомления через сигнального бота. None без ключей биржи."""
    import os

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        return None
    from oskar.core.position_monitor import PositionMonitor
    from oskar.data.client import make_bingx_exchange
    from oskar.execution.bingx import BingXAdapter

    adapter = BingXAdapter(make_bingx_exchange(key, secret, sandbox=_sandbox(settings)))
    notify = on_expired = None
    if token and chat_id:
        from oskar.core.telegram_out import TelegramSender

        sender = TelegramSender(token, chat_id)
        notify = sender.send_text

        def on_expired(decision_id, message_id):
            if message_id:
                sender.remove_buttons(message_id)

    return PositionMonitor(
        adapter, state.journal,
        max_loss_hold_seconds=profile.max_loss_hold_seconds,
        limit_entry_ttl_seconds=profile.limit_entry_ttl_seconds,
        notify=notify, on_card_expired=on_expired, state=state,
    )


def _make_equity_fn(settings):
    """Equity с биржи при наличии ключей; None-фоллбек внутри runner'а.
    Среда — та же, что у исполнения (раньше sandbox был зашит True: реальные
    ордера размером от VST-баланса)."""
    import os

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        return None
    from oskar.sync.exchange_sync import make_bingx_view, snapshot_balance

    view = make_bingx_view(key, secret, sandbox=_sandbox(settings))

    def equity() -> float | None:
        try:
            return snapshot_balance(view)["total"]
        except Exception:  # noqa: BLE001 - сеть/биржа: остаемся на прежнем значении
            return None

    return equity


def main() -> int:
    from oskar._io import force_utf8_stdio

    force_utf8_stdio()
    import logging

    from oskar.core.api import build_app
    from oskar.core.bootstrap import (
        build_cycle_config,
        load_active_profile,
        load_caps,
        load_scanner_settings,
        load_settings,
    )
    from oskar.core.cycle import ShadowCycle
    from oskar.core.shadow_runner import ShadowRunner
    from oskar.core.state import RuntimeState
    from oskar.data.candles import CandleCache
    from oskar.data.client import MarketDataClient, make_bingx_public_exchange
    from oskar.ops.caps_guard import verify_caps

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx пишет в INFO URL каждого запроса, а URL Telegram содержит токен бота —
    # в лог нельзя (утечка поймана 2026-10-12 на первом же getUpdates, лог вычищен)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not verify_caps():
        print("REFUSING TO START: caps.yaml does not match CONSTITUTION.md pin (Art. 4)", flush=True)
        return 2
    try:
        lock = acquire_instance_lock(REPO_ROOT / "data" / "oskard.lock")  # noqa: F841 - держим до выхода
        check_port_free()
    except SingleInstanceError as e:
        print(f"REFUSING TO START: {e}", flush=True)
        return 3

    caps = load_caps()
    profile = load_active_profile()
    settings = load_settings()
    scanner = load_scanner_settings(settings)
    cycle_cfg = build_cycle_config(profile, scanner, settings)

    # одна среда для исполнения, equity и монитора; реал закрыт гейтом Фазы 5
    mode = str(settings.get("mode", "paper"))
    if mode == "live":
        print("REFUSING TO START: mode=live is gated until Phase 5 (PF>=1.3 over 60 trades)", flush=True)
        return 6
    if mode == "paper" and not _sandbox(settings):
        print("REFUSING TO START: mode=paper but exchange.sandbox=false (real orders from a paper run)",
              flush=True)
        return 6

    journal = _connect_journal()
    if journal is None:
        return 5
    state = RuntimeState(journal=journal)

    import os
    import secrets

    from dotenv import load_dotenv

    from oskar.core.api import panic_engaged

    load_dotenv(REPO_ROOT / ".env")  # ДО чтения Telegram-переменных: иначе поллер и алерты молча OFF
    signals_token = os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    alert = None
    alert_token = signals_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if alert_token and chat_id:
        from oskar.core.telegram_out import TelegramSender

        alert = TelegramSender(alert_token, chat_id).send_text

    engaged = panic_engaged(journal)
    if engaged:
        state.panic()  # ст. 6 п. 3: после рестарта panic держится до явного сброса
        print(f"PANIC ENGAGED since {engaged}: trading blocked until POST /panic/reset", flush=True)

    client = MarketDataClient(make_bingx_public_exchange())
    cache = CandleCache(client)
    cycle = ShadowCycle(cache, state, caps, profile.limits, cycle_cfg)
    runner = ShadowRunner(
        cycle, cache, client, state,
        top_n=scanner.full_analysis_top,
        min_quote_volume=scanner.min_hourly_quote_volume_usd,
        tick_seconds=float(profile.scan_interval_seconds),
        equity_fn=_make_equity_fn(settings),
        notify=alert,
    )
    cycle._escalator = _make_escalator(state, caps, profile, settings, runner, cache)
    runner.start_thread()

    poller = None
    if signals_token and chat_id:
        from oskar.core.telegram_buttons import ButtonPoller

        executor = _make_executor(state, settings, caps, client)
        on_resolution = None
        if executor is not None:
            from oskar.core.executor import equity_guarded

            on_resolution = equity_guarded(
                executor.execute, lambda: runner.equity_source,
                equity_fn=lambda: runner.equity_usd, plan_fn=executor.plan_for,
                risk_cap_pct=caps.risk_per_trade_pct,
            )
        poller = ButtonPoller(signals_token, chat_id, state, on_resolution=on_resolution)
        poller.start_thread()
        print(f"button poller: ON (signals bot), execution: "
              f"{'ON' if executor is not None else 'OFF (no keys)'}", flush=True)

    monitor = _make_monitor(state, settings, profile, alert_token, chat_id)
    if monitor is not None:
        monitor.start_thread()
    print(f"position monitor: {'ON' if monitor is not None else 'OFF (no keys)'}", flush=True)

    # токен API: страница в браузере на этой же машине не должна жать panic/approve
    api_token = os.getenv("OSKAR_API_TOKEN", "") or secrets.token_hex(16)
    token_path = REPO_ROOT / "data" / "oskard.token"
    token_path.write_text(api_token, encoding="ascii")  # читают Hermes-скиллы и CLI
    app = build_app(state, shadow=runner, monitor=monitor, api_token=api_token)
    print(
        f"oskard starting: run_id={state.run_id} profile={profile.name} "
        f"mode={mode} sandbox={_sandbox(settings)} api=http://{DEFAULT_HOST}:{DEFAULT_PORT} "
        f"(token in {token_path.name})", flush=True,
    )

    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="warning")
    runner.stop()
    if poller is not None:
        poller.stop()
    if monitor is not None:
        monitor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
