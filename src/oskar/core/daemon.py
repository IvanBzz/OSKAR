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


def main() -> int:
    from oskar.core.api import build_app
    from oskar.core.state import RuntimeState
    from oskar.journal.db import connect
    from oskar.ops.caps_guard import verify_caps

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
    app = build_app(state)
    print(f"oskard starting: run_id={state.run_id} api=http://{DEFAULT_HOST}:{DEFAULT_PORT}")

    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
