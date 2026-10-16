"""Гейт среды: до Фазы 5 торговать можно ТОЛЬКО на paper/VST (P0, гейт реальных денег).

Блок H, 2026-10-16: демон проверял среду черным списком — отказ при mode == "live" и при
paper + sandbox=false. Любое другое значение («real», «Live», опечатка) вместе с
sandbox: false запускало демон на реальном BingX. Теперь белый список, одна функция для
всех мест, где читаются mode/sandbox: старт демона, фабрики приватного доступа к бирже,
допуск автомата, CLI. Разрешено ровно одно: mode == "paper" (после strip) и
exchange.sandbox — именно bool True (строка "true" из кривого YAML — отказ).
"""

PHASE5_GATE = "реальная торговля закрыта гейтом Фазы 5 (60 сделок, PF ≥ 1.3)"


def paper_gate(settings: dict | None) -> str | None:
    """None — среда разрешена (paper + BingX VST); иначе причина отказа."""
    settings = settings or {}
    mode = settings.get("mode")
    if not (isinstance(mode, str) and mode.strip() == "paper"):
        return f"{PHASE5_GATE}; режим {mode!r} не разрешен"
    sandbox = (settings.get("exchange") or {}).get("sandbox")
    if sandbox is not True:
        return f"{PHASE5_GATE}; exchange.sandbox={sandbox!r} не разрешен (нужен true — BingX VST)"
    return None


def require_vst(sandbox) -> None:
    """Фабрики приватного доступа: только VST (sandbox именно True), иначе исключение —
    не тихий переход на реальный хост."""
    if sandbox is not True:
        raise RuntimeError(f"{PHASE5_GATE}; приватный доступ к бирже только через VST, sandbox={sandbox!r}")
