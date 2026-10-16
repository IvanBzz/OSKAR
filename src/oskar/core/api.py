"""Управляющий API демона — 127.0.0.1:8321 (бинд порта = single-instance lock).

Панель для Hermes-скиллов (oskar-control, oskar-approvals) и CLI. Никакой
торговой логики здесь: approve/reject меняют pending_approvals; исполнение
подхватывает цикл (Фаза 2). Истекший TTL необратим — fail-closed.

Защита (ревью 2026-10-13): loopback не защищает от страницы в браузере на той
же машине — POST без preflight мог снять паузу или нажать panic/approve.
С api_token каждый запрос обязан нести X-Oskar-Token, Host ограничен
loopback-именами, автодокументация выключена.

panic / panic-reset — тонкие обертки над core/emergency.py: та же функция
обслуживает Telegram `/panic` и CLI `oskar panic` (ст. 6, ревью 2026-10-13).
drawdown/reset — ручное снятие стопа по просадке (risk/drawdown.py, блок H 2026-10-16).
"""

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from oskar import __version__
from oskar.core.emergency import (  # noqa: F401 - PANIC_META_KEY/panic_engaged: реэкспорт старых импортов
    PANIC_META_KEY,
    engage_panic,
    panic_engaged,
    reset_panic,
)
from oskar.core.state import RuntimeState
from oskar.risk.drawdown import DrawdownError, drawdown_status


def build_app(state: RuntimeState, shadow=None, monitor=None, api_token: str | None = None,
              trading_mode=None, drawdown=None) -> FastAPI:
    hardened = bool(api_token)

    def require_token(x_oskar_token: str = Header(default="")):
        if hardened and not secrets.compare_digest(x_oskar_token, api_token):
            raise HTTPException(401, "missing or invalid X-Oskar-Token")

    app = FastAPI(
        title="oskard", version=__version__,
        dependencies=[Depends(require_token)],
        docs_url=None if hardened else "/docs",
        redoc_url=None, openapi_url=None if hardened else "/openapi.json",
    )
    if hardened:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/status")
    def status():
        open_pending = state.journal.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE resolution IS NULL"
        ).fetchone()[0]
        decisions = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        by_status = dict(state.journal.execute(
            "SELECT status, COUNT(*) FROM trades GROUP BY status"
        ).fetchall())
        payload = {
            "run_id": state.run_id,
            "version": __version__,
            "paused": state.paused,
            "safe_mode": state.safe_mode,
            "panic_at": state.panic_at,
            "drawdown": drawdown_status(state.journal),  # пик equity и защелка просадки
            "uptime_sec": state.uptime_sec,
            "pending_approvals": open_pending,
            "decisions_total": decisions,
            "trades": {
                "pending": by_status.get("pending", 0),
                "open": by_status.get("open", 0),
                "closed": by_status.get("closed", 0),
                "cancelled": by_status.get("cancelled", 0),
                "monitor": (
                    {"ticks": monitor.ticks, "last": monitor.last_report}
                    if monitor is not None else None
                ),
            },
        }
        if shadow is not None:
            payload["shadow"] = {
                "mode": "shadow",
                "watchlist": len(shadow.watchlist),
                "ticks": shadow.ticks,
                "equity_usd": round(shadow.equity_usd, 2),
                "equity_source": shadow.equity_source,
                "equity_at": shadow.equity_at,
                "last_report": shadow.last_report,
            }
            # здоровье данных: universe/отрезанное, черный список, cooldown kline-API
            health = getattr(shadow, "data_health", None)
            payload["data"] = health() if callable(health) else {}
        return payload

    @app.get("/pending")
    def pending():
        rows = state.journal.execute(
            "SELECT decision_id, card_json, expires_at, created_at "
            "FROM pending_approvals WHERE resolution IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def _resolve(decision_id: str, action: str) -> dict:
        from oskar.core.approvals import resolve_approval

        out = resolve_approval(
            state.journal, decision_id, action,
            paused=state.paused, safe_mode=state.safe_mode,
        )
        if not out.ok:
            raise HTTPException(out.code, out.message)
        return {"decision_id": decision_id, "resolution": out.resolution}

    @app.post("/decisions/{decision_id}/approve")
    def approve(decision_id: str):
        return _resolve(decision_id, "approve")

    @app.post("/decisions/{decision_id}/reject")
    def reject(decision_id: str):
        return _resolve(decision_id, "reject")

    @app.post("/decisions/{decision_id}/snooze")
    def snooze(decision_id: str):
        return _resolve(decision_id, "snooze")

    @app.post("/pause")
    def pause():
        state.paused = True
        return {"paused": True}

    @app.post("/resume")
    def resume():
        if state.panic_at is not None:
            raise HTTPException(423, "panic engaged: manual operator reset required")
        state.paused = False
        return {"paused": False}

    @app.get("/mode")
    def get_mode():
        if trading_mode is None:
            return {"mode": "confirm", "auto_allowed": False}
        return {"mode": trading_mode.get(), "auto_allowed": trading_mode.allowed}

    @app.post("/mode/{mode}")
    def set_mode(mode: str):
        """Переключатель «автомат / с подтверждением» (то же, что /mode в боте)."""
        if trading_mode is None:
            raise HTTPException(409, "режим не подключен (нет ключей биржи)")
        try:
            return {"mode": trading_mode.set(mode)}
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/panic")
    def panic():
        # ст. 6: флаги -> meta -> биржа; та же функция у Telegram /panic и CLI
        return engage_panic(state, monitor)

    @app.post("/panic/reset")
    def panic_reset():
        """Ст. 6 п. 3: только явной командой оператора. Снимает panic и safe_mode,
        пауза остается — resume отдельным решением."""
        return reset_panic(state)

    @app.post("/drawdown/reset")
    def drawdown_reset():
        """Стоп по просадке снимается только явной командой: защелка — прочь, пик := текущий
        equity с биржи (без него — 409, ничего не тронуто)."""
        if drawdown is None:
            raise HTTPException(409, "стоп по просадке не подключен")
        try:
            return drawdown.reset()
        except DrawdownError as e:
            raise HTTPException(409, str(e)) from e

    return app
