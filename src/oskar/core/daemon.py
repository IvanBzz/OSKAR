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


def _connect_journal():
    """Журнал; при несовпадении версии схемы — архив старого и чистый старт
    (допустимо ТОЛЬКО в shadow-фазе: реальных сделок в базе нет)."""
    from oskar.journal.archive import archive_journal
    from oskar.journal.db import connect

    db_path = REPO_ROOT / "data" / "oskar.db"
    try:
        return connect(db_path)
    except RuntimeError as e:
        snap = archive_journal(db_path)
        for suffix in ("", "-wal", "-shm"):
            p = db_path.parent / (db_path.name + suffix)
            p.unlink(missing_ok=True)
        print(f"journal schema mismatch ({e}); old journal archived to {snap}")
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
                png = render_chart(df, candidate)
            except Exception:  # noqa: BLE001 - карточка без графика лучше отсутствия
                pass
            sender.send_setup_card(candidate, verdict, png, ttl_min, scout_verdict, plan)
    else:
        print("escalation cards OFF: telegram not configured")

    from oskar.core.bootstrap import load_caps

    return Escalator(
        state, load_caps(), profile_settings.limits,
        scout_fn=scout_fn, trader_fn=trader_fn, resolve_fn=resolve_prices,
        approval_ttl_seconds=profile_settings.approval_ttl_seconds,
        card_sink=card_sink,
        equity_fn=lambda: runner.equity_usd,
    )


def _make_executor(state, settings):
    """Исполнитель по кнопке. Режим из settings.mode: paper -> VST sandbox."""
    import os

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        return None
    sandbox = bool(settings.get("exchange", {}).get("sandbox", True))
    from oskar.core.executor import ExecutionService
    from oskar.data.client import make_bingx_exchange
    from oskar.execution.bingx import BingXAdapter

    adapter = BingXAdapter(make_bingx_exchange(key, secret, sandbox=sandbox))
    return ExecutionService(adapter, state.journal, state.run_id)


def _make_equity_fn():
    """Equity с биржи (VST) при наличии ключей; None-фоллбек внутри runner'а."""
    import os

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        return None
    from oskar.sync.exchange_sync import make_bingx_view, snapshot_balance

    view = make_bingx_view(key, secret, sandbox=True)

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
        print("REFUSING TO START: caps.yaml does not match CONSTITUTION.md pin (Art. 4)")
        return 2
    try:
        check_port_free()
    except SingleInstanceError as e:
        print(f"REFUSING TO START: {e}")
        return 3

    journal = _connect_journal()
    state = RuntimeState(journal=journal)

    caps = load_caps()
    profile = load_active_profile()
    settings = load_settings()
    scanner = load_scanner_settings(settings)
    cycle_cfg = build_cycle_config(profile, scanner, settings)

    client = MarketDataClient(make_bingx_public_exchange())
    cache = CandleCache(client)
    cycle = ShadowCycle(cache, state, caps, profile.limits, cycle_cfg)
    runner = ShadowRunner(
        cycle, cache, client, state,
        top_n=scanner.full_analysis_top,
        min_quote_volume=scanner.min_hourly_quote_volume_usd,
        tick_seconds=float(profile.scan_interval_seconds),
        equity_fn=_make_equity_fn(),
    )
    cycle._escalator = _make_escalator(state, caps, profile, settings, runner, cache)
    runner.start_thread()

    import os

    signals_token = os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if signals_token and chat_id:
        from oskar.core.telegram_buttons import ButtonPoller

        executor = _make_executor(state, settings)
        on_resolution = None
        if executor is not None:
            from oskar.core.executor import equity_guarded

            on_resolution = equity_guarded(executor.execute, lambda: runner.equity_source)
        poller = ButtonPoller(signals_token, chat_id, state, on_resolution=on_resolution)
        poller.start_thread()
        print(f"button poller: ON (signals bot), execution: "
              f"{'ON' if executor is not None else 'OFF (no keys)'}")

    app = build_app(state, shadow=runner)
    print(
        f"oskard starting: run_id={state.run_id} profile={profile.name} "
        f"mode=shadow api=http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    )

    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="warning")
    runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
