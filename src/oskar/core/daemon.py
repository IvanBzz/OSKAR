"""oskard — точка входа демона.

Порядок старта (каждый шаг обязателен, отказ = не стартуем):
1. Проверка капсов против конституции (ст. 4) — mismatch = отказ запуска
2. Single-instance lock: файловый замок + пробный бинд порта API (settings.api,
   по умолчанию 127.0.0.1:8321) — занят = второй экземпляр, громкая ошибка
   (v1: два экземпляра слили ADA в двойную позицию)
3. Журнал (settings.journal.db_path) + RuntimeState (run_id, запуск записан в meta)
4. Проверка биржи ДО первого входа (блок I, 2026-10-16): свои сироты — снять, ордера/позиции
   OSKAR чужого запуска (вторая машина?) — SAFE MODE, ничего не снимаем; затем потоки
   runner / монитор / ops (инварианты, дневная сводка) / кнопки
5. uvicorn с управляющим API; при выходе — stop + join фоновых потоков

Торговый цикл (ShadowCycle) в Фазе 1 запускается отдельно/вручную;
фоновый поток цикла подключается вместе с исполнением в Фазе 2.
"""

import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
DEFAULT_JOURNAL = "data/oskar.db"
# TrustedHost API пускает только эти имена (core/api.py); иной бинд = panic/approve в сети
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
THREAD_JOIN_TIMEOUT_SEC = 5.0


class SingleInstanceError(RuntimeError):
    pass


def api_endpoint(settings: dict) -> tuple[str, int]:
    """Адрес API из settings.api. Ревью 2026-10-13 (дрейф конфига): ключи api.*
    не читались — адрес был зашит. Только loopback, иначе ValueError = отказ старта."""
    api = settings.get("api") or {}
    host = str(api.get("host", DEFAULT_HOST))
    port = int(api.get("port", DEFAULT_PORT))
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"api.host={host!r} is not loopback ({'/'.join(LOOPBACK_HOSTS)}): "
            "the control API (panic/approve) must never listen on the network"
        )
    if not 0 < port < 65536:
        raise ValueError(f"api.port={port} is out of range")
    return host, port


def journal_path(settings: dict) -> Path:
    """journal.db_path из settings.yaml (ревью 2026-10-13: ключ не читался, путь был
    зашит в _connect_journal). Относительный путь — от корня репо."""
    return REPO_ROOT / str((settings.get("journal") or {}).get("db_path", DEFAULT_JOURNAL))


def stop_and_join(workers, timeout: float = THREAD_JOIN_TIMEOUT_SEC) -> list[str]:
    """Остановка фоновых потоков при выходе. Ревью 2026-10-13: был только stop() —
    процесс выходил сразу, daemon-потоки обрывались посреди тика (ордер ушел, запись
    в журнал — нет). Сначала stop() всем (сворачиваются параллельно), затем join
    с таймаутом на каждый. workers: пары (объект со stop() | None, Thread | None).
    Возвращает имена потоков, не уложившихся в таймаут."""
    alive = [(worker, thread) for worker, thread in workers if worker is not None]
    for worker, _ in alive:
        try:
            worker.stop()
        except Exception as e:  # noqa: BLE001 - остальные все равно останавливаем
            print(f"WARNING: stop failed: {e}", flush=True)
    stuck = []
    for _, thread in alive:
        if thread is None:
            continue
        thread.join(timeout)
        if thread.is_alive():
            stuck.append(thread.name)
    return stuck


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


def _connect_journal(db_path: Path):
    """Журнал; при несовпадении версии схемы — архив старого и чистый старт.
    Допустимо ТОЛЬКО без живых сделок: с open/pending в базе отказываемся
    стартовать (иначе теряются order id и управление позициями). None = отказ."""
    from oskar.journal.archive import archive_journal
    from oskar.journal.db import connect

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


def _make_escalator(state, caps, profile_settings, settings, runner, cache, auto_executor=None):
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

        def card_sink(candidate, verdict, scout_verdict, plan=None, note=None, buttons=True):  # noqa: F811
            png = None
            try:
                df = cache.get(candidate.symbol, candidate.timeframe, 200)
                png = render_chart(df, candidate, plan)
            except Exception:  # noqa: BLE001 - карточка без графика лучше отсутствия
                pass
            sent = sender.send_setup_card(candidate, verdict, png, ttl_min, scout_verdict, plan,
                                          note=note, buttons=buttons)
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
        auto_executor=auto_executor,
    )


def _sandbox(settings) -> bool:
    """Среда приватного доступа к бирже: до гейта Фазы 5 — только VST. Белый список
    (блок H, 2026-10-16): вне paper + sandbox=true — исключение, а не реальный хост
    (было bool(...) от любого значения ключа)."""
    from oskar.core.env_gate import paper_gate

    reason = paper_gate(settings)
    if reason is not None:
        raise RuntimeError(reason)
    return True


def _make_executor(state, settings, caps, client, profile_limits=None, notify=None):
    """Исполнитель по кнопке. Среда (VST/реал) — из settings.exchange.sandbox,
    та же, что у equity и монитора. Цена для проверки «план не устарел» — с
    публичного тикера; лимит позиций — из капсов. Капсы и профиль — еще и для
    перепроверки R:R/стопа по фактической цене входа (блок H, 2026-10-16), state —
    panic/SAFE MODE непосредственно перед отправкой входа."""
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
        caps=caps, profile=profile_limits, state=state, notify=notify,  # алерт fill_out_of_caps
    )


def _make_monitor(state, settings, profile, token: str, chat_id: str, drawdown=None):
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
    from oskar.sync.reconcile import ClosedTradeReconciler

    exchange = make_bingx_exchange(key, secret, sandbox=_sandbox(settings))
    adapter = BingXAdapter(exchange)
    notify = on_expired = None
    if token and chat_id:
        from oskar.core.telegram_out import TelegramSender

        sender = TelegramSender(token, chat_id)
        notify = sender.send_text

        def on_expired(decision_id, message_id):
            if message_id:
                sender.remove_buttons(message_id)

    # ст. 1 п. 1: закрытые сделки сверяются по филлам биржи — сразу после закрытия и раз в 6 ч
    reconciler = ClosedTradeReconciler(exchange, state.journal, run_id=state.run_id, notify=notify)
    return PositionMonitor(
        adapter, state.journal,
        max_loss_hold_seconds=profile.max_loss_hold_seconds,
        limit_entry_ttl_seconds=profile.limit_entry_ttl_seconds,
        notify=notify, on_card_expired=on_expired, state=state, reconciler=reconciler,
        drawdown=drawdown,
    )


def _make_digest_fn(state, runner, trading_mode, settings, profile, cfg):
    """Текст дневной сводки (блок I, 2026-10-16): журнал + состояние демона + свежие вердикты
    инвариантов (без алертов — их шлет InvariantWatch). То же у /digest в боте."""
    from datetime import UTC, datetime

    from oskar.core.emergency import safe_mode_engaged
    from oskar.ops.digest import RuntimeSnapshot, build_digest
    from oskar.ops.invariants import evaluate

    a = settings.get("agents", {})
    budgets = {
        "scout": int(a.get("scout", {}).get("daily_budget", 30)),
        "trader": int(a.get("trader", {}).get("daily_budget", 20)),
    }

    def digest() -> str:
        now = datetime.now(UTC)
        try:
            verdicts = evaluate(state.journal, now, max_loss_hold_seconds=profile.max_loss_hold_seconds)
        except Exception as e:  # noqa: BLE001 - сводка без вердиктов лучше, чем без сводки
            print(f"WARNING: digest invariants failed: {e}", flush=True)
            verdicts = None
        info = safe_mode_engaged(state.journal) if state.safe_mode else None
        snap = RuntimeSnapshot(
            run_id=state.run_id, uptime_sec=state.uptime_sec, runner_ticks=runner.ticks,
            safe_mode=state.safe_mode, safe_reason=(info or {}).get("reason") or None,
            paused=state.paused, panic=state.panic_at is not None,
            mode=trading_mode.get() if trading_mode is not None else None,
            equity_usd=runner.equity_usd, equity_source=runner.equity_source, budgets=budgets,
        )
        return build_digest(state.journal, now, snap, verdicts=verdicts,
                            local_now=now.astimezone(cfg.tz), tz_label=cfg.tz_label)

    return digest


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
    from oskar.logging_setup import setup_logging
    from oskar.ops.caps_guard import verify_caps

    # Ротация 10 МБ x 5 + вычистка токенов/подписей (ревью 2026-10-13). Запуск теперь
    # обязан писать stdout/stderr в data/oskard.stdout.log: два писателя на одном файле
    # ломают ротацию (Windows не переименует файл, открытый редиректом).
    setup_logging(REPO_ROOT / "data" / "oskard.log")

    if not verify_caps():
        print("REFUSING TO START: caps.yaml does not match CONSTITUTION.md pin (Art. 4)", flush=True)
        return 2
    settings = load_settings()  # до замка: порт API для пробного бинда — из settings.api
    try:
        api_host, api_port = api_endpoint(settings)
    except ValueError as e:
        print(f"REFUSING TO START: {e}", flush=True)
        return 6
    try:
        lock = acquire_instance_lock(REPO_ROOT / "data" / "oskard.lock")  # noqa: F841 - держим до выхода
        check_port_free(api_host, api_port)
    except SingleInstanceError as e:
        print(f"REFUSING TO START: {e}", flush=True)
        return 3

    caps = load_caps()
    profile = load_active_profile()
    scanner = load_scanner_settings(settings)
    cycle_cfg = build_cycle_config(profile, scanner, settings)
    from oskar.core.ops_jobs import ops_config

    try:
        ops_cfg = ops_config(settings)  # блок I: сводка и инварианты по расписанию
    except ValueError as e:
        print(f"REFUSING TO START: {e}", flush=True)
        return 6

    # одна среда для исполнения, equity и монитора; реал закрыт гейтом Фазы 5. Белый список
    # (блок H, 2026-10-16): был черный — «real»/«Live»/опечатка + sandbox:false шли на реал.
    from oskar.core.env_gate import paper_gate

    gate = paper_gate(settings)
    if gate is not None:
        print(f"REFUSING TO START: {gate}", flush=True)
        return 6
    mode = "paper"

    journal = _connect_journal(journal_path(settings))
    if journal is None:
        return 5
    from oskar.core.emergency import SAFE_MODE_FLAG_NAME

    # маркер-файл — второй носитель SAFE MODE (ревью Codex R1, 2026-10-16)
    state = RuntimeState(journal=journal, safe_mode_flag=REPO_ROOT / "data" / SAFE_MODE_FLAG_NAME)
    # Запуск известен журналу с первой секунды: его сироты — «свои» (снимаются), ордера
    # OSKAR с незнакомым run_id — чужая машина (блок I, 2026-10-16, core/run_registry.py)
    from oskar.core.run_registry import register_run

    try:
        register_run(journal, state.run_id)
    except Exception as e:  # noqa: BLE001 - без записи сироты этого запуска сочтутся чужими (не снимем)
        print(f"WARNING: run {state.run_id} not registered in journal: {e}", flush=True)

    import os
    import secrets

    from dotenv import load_dotenv

    from oskar.core.emergency import engage_panic, panic_engaged

    load_dotenv(REPO_ROOT / ".env")  # ДО чтения Telegram-переменных: иначе поллер и алерты молча OFF
    signals_token = os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    alert = alert_sender = None
    alert_token = signals_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if alert_token and chat_id:
        from oskar.core.telegram_out import TelegramSender

        alert_sender = TelegramSender(alert_token, chat_id)  # тот же бот, что шлет карточки
        alert = alert_sender.send_text

    engaged = panic_engaged(journal)
    if engaged:
        state.panic()  # ст. 6 п. 3: после рестарта panic держится до явного сброса
        print(f"PANIC ENGAGED since {engaged}: trading blocked until `oskar panic-reset` "
              "(POST /panic/reset)", flush=True)
    # SAFE MODE тоже переживает рестарт (блок H, 2026-10-16: супервизор снимал бы его молча);
    # по ЛЮБОМУ носителю — meta или маркер-файл (ревью Codex R1)
    from oskar.core.emergency import restore_safe_mode

    safe_reason = restore_safe_mode(state, notify=alert)
    if safe_reason is not None:
        print(f"SAFE MODE restored after restart ({safe_reason!r}): new entries blocked until "
              "`oskar panic-reset`", flush=True)

    client = MarketDataClient(make_bingx_public_exchange())
    cache = CandleCache(client)
    cycle = ShadowCycle(cache, state, caps, profile.limits, cycle_cfg)
    # Equity с биржи с моментом ответа (ревью Codex R1, 2026-10-16): тот же snapshot_balance для
    # runner'а (час), монитора (защелка на каждом полном тике) и исполнителя (не старше 120 с)
    from oskar.risk.drawdown import DEFAULT_DRAWDOWN_PCT, DrawdownGuard, LiveEquity

    equity_fetch = _make_equity_fn(settings)
    live_equity = LiveEquity(equity_fetch) if equity_fetch is not None else None
    runner = ShadowRunner(
        cycle, cache, client, state,
        top_n=scanner.full_analysis_top,
        min_quote_volume=scanner.min_hourly_quote_volume_usd,
        tick_seconds=float(profile.scan_interval_seconds),
        # ключ не читался — runner жил на зашитом дефолте того же значения (ревью 2026-10-13)
        consecutive_errors_to_safe_mode=int((settings.get("safe_mode") or {}).get("consecutive_errors", 3)),
        equity_fn=live_equity.refresh if live_equity is not None else None,
        notify=alert,
    )
    # Стоп по просадке (блок H, 2026-10-16): по свежему equity с биржи, не по кешу runner'а
    try:
        drawdown = DrawdownGuard(
            journal, float((settings.get("safe_mode") or {}).get("drawdown_pct", DEFAULT_DRAWDOWN_PCT)),
            equity_fn=lambda: runner.equity_usd, equity_source_fn=lambda: runner.equity_source,
            live=live_equity,
        )
    except (TypeError, ValueError) as e:
        print(f"REFUSING TO START: safe_mode.drawdown_pct: {e}", flush=True)
        return 6
    # Исполнитель и режим — ДО эскалатора: в режиме «автомат» сделку ставит сам цикл.
    from oskar.core.trading_mode import AutoExecutor, TradingMode

    executor = _make_executor(state, settings, caps, client, profile.limits, notify=alert)
    guarded_execute = None
    if executor is not None:
        from oskar.core.executor import equity_guarded

        guarded_execute = equity_guarded(
            executor.execute, lambda: runner.equity_source,
            equity_fn=lambda: runner.equity_usd, plan_fn=executor.plan_for,
            risk_cap_pct=caps.risk_per_trade_pct,
            live=live_equity, drawdown=drawdown,  # свежий equity и защелка — до входа (ревью Codex R1)
        )
    # автомат — только paper/VST (реал закрыт гейтом Фазы 5); по умолчанию — из настроек
    trading_mode = TradingMode(
        journal, allowed=(paper_gate(settings) is None and guarded_execute is not None),
        default=str((settings.get("execution") or {}).get("default_mode", "confirm")),
    )
    auto_executor = (AutoExecutor(trading_mode, state, guarded_execute, notify=alert)
                     if guarded_execute is not None else None)
    print(f"execution mode: {trading_mode.get()} (auto allowed: {trading_mode.allowed})", flush=True)
    cycle._escalator = _make_escalator(state, caps, profile, settings, runner, cache,
                                       auto_executor=auto_executor)

    # Монитор — ДО поллера: Telegram /panic зовет monitor.panic_flatten(), а /panic,
    # пролежавший в очереди Telegram, поллер исполнит на первом же опросе.
    monitor = _make_monitor(state, settings, profile, alert_token, chat_id, drawdown=drawdown)
    if monitor is not None:
        # Блок I, 2026-10-16: сироты и позиции без сделки — ДО первого входа (runner еще не
        # запущен). Ордера OSKAR чужого запуска (вторая машина?) -> SAFE MODE, ничего не снимаем.
        startup = monitor.startup_check()
        print(f"startup exchange check: orphans cancelled {len(startup.orphans)}, "
              f"untracked positions {len(startup.mismatches)}, errors {len(startup.errors)}"
              f"{' - SAFE MODE' if state.safe_mode else ''}", flush=True)
    runner_thread = runner.start_thread()
    monitor_thread = monitor.start_thread() if monitor is not None else None
    print(f"position monitor: {'ON' if monitor is not None else 'OFF (no keys)'}", flush=True)

    # Инварианты по расписанию + дневная сводка (блок I, 2026-10-16)
    from oskar.core.ops_jobs import InvariantWatch, OpsScheduler

    digest_fn = _make_digest_fn(state, runner, trading_mode, settings, profile, ops_cfg)
    ops = OpsScheduler(
        journal, ops_cfg,
        watch=InvariantWatch(journal, alert, max_loss_hold_seconds=profile.max_loss_hold_seconds),
        digest_fn=digest_fn, notify=alert,
    )
    ops_thread = ops.start_thread()
    print(f"ops: invariants every {ops_cfg.invariants_every}, digest "
          f"{ops_cfg.digest_at.strftime('%H:%M')} {ops_cfg.tz_name} "
          f"({'ON' if alert is not None else 'OFF: telegram not configured'})", flush=True)

    poller = poller_thread = None
    if signals_token and chat_id:
        from oskar.core.telegram_buttons import ButtonPoller, status_line

        on_resolution = guarded_execute

        def on_mode(target):
            return trading_mode.set(target) if target else trading_mode.get()

        # ст. 6: Telegram /panic — та же функция, что у API и CLI (флаги -> meta -> биржа)
        poller = ButtonPoller(
            signals_token, chat_id, state, on_resolution=on_resolution,
            on_panic=lambda: engage_panic(state, monitor),
            on_status=lambda: f"{status_line(state, runner)} · режим: {trading_mode.get()}",
            on_mode=on_mode if auto_executor is not None else None,
            on_digest=digest_fn,
        )
        poller_thread = poller.start_thread()
        print(f"button poller: ON (signals bot, /mode /panic /status /digest), execution: "
              f"{'ON' if executor is not None else 'OFF (no keys)'}", flush=True)

    def card_done(decision_id: str) -> None:
        """approve через API закрыл решение — снять кнопки с карточки (нажимать нечего)."""
        row = journal.execute(
            "SELECT telegram_message_id FROM pending_approvals WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if alert_sender is not None and row is not None and row[0]:
            alert_sender.remove_buttons(row[0])

    # токен API: страница в браузере на этой же машине не должна жать panic/approve
    api_token = os.getenv("OSKAR_API_TOKEN", "") or secrets.token_hex(16)
    token_path = REPO_ROOT / "data" / "oskard.token"
    token_path.write_text(api_token, encoding="ascii")  # читают Hermes-скиллы и CLI
    # approve через API (Hermes) — тот же охраняемый исполнитель, что у кнопок и автомата
    app = build_app(state, shadow=runner, monitor=monitor, api_token=api_token,
                    trading_mode=trading_mode, drawdown=drawdown,
                    execute_fn=guarded_execute, on_card_done=card_done)
    print(
        f"oskard starting: run_id={state.run_id} profile={profile.name} "
        f"mode={mode} sandbox={_sandbox(settings)} api=http://{api_host}:{api_port} "
        f"(token in {token_path.name})", flush=True,
    )

    import uvicorn

    # log_config=None: uvicorn не ставит свои хендлеры в stderr, его WARNING+ идут
    # через корневые — с ротацией и вычисткой секретов
    uvicorn.run(app, host=api_host, port=api_port, log_level="warning", log_config=None)
    stuck = stop_and_join([(runner, runner_thread), (poller, poller_thread), (monitor, monitor_thread),
                           (ops, ops_thread)])
    if stuck:
        print(f"shutdown: still busy after {THREAD_JOIN_TIMEOUT_SEC:.0f}s: {', '.join(stuck)} "
              "(daemon threads end with the process)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
