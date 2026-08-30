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
    from oskar.journal.db import connect
    from oskar.ops.caps_guard import verify_caps

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not verify_caps():
        print("REFUSING TO START: caps.yaml does not match CONSTITUTION.md pin (Art. 4)")
        return 2
    try:
        check_port_free()
    except SingleInstanceError as e:
        print(f"REFUSING TO START: {e}")
        return 3

    journal = connect(REPO_ROOT / "data" / "oskar.db")
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
    runner.start_thread()

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
