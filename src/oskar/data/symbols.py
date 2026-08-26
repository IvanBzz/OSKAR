"""Нормализатор символов — ЕДИНСТВЕННАЯ точка преобразования форматов.

Урок v1: LLM и биржа возвращали SOLUSDT / SOL-USDT / SOL/USDT / BTC/USDT:USDT
недетерминированно; проверка дублей молча пропускала ADA/USDT:USDT vs ADA-USDT
и две ADA SHORT слились в одну 2x позицию.

Канон OSKAR = унифицированный ccxt-формат перпа: "SOL/USDT:USDT".
Все границы (LLM-ответы, биржевые ответы, журнал, конфиг) обязаны проходить
через canonical() НА ВХОДЕ. Внутри системы других форматов не существует.
Спот пока не поддерживается (торгуем только USDT-перпы).
"""

KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "USD", "BTC", "ETH")
_SEPARATORS = ("/", "-", "_")


class SymbolError(ValueError):
    pass


def canonical(raw: str) -> str:
    """Любой встречавшийся формат -> "BASE/QUOTE:SETTLE" (перп, settle=quote).

    SOLUSDT | SOL-USDT | sol/usdt | SOL/USDT:USDT | 1000PEPEUSDT -> канон.
    """
    if not raw or not raw.strip():
        raise SymbolError("empty symbol")
    s = raw.strip().upper().replace(" ", "")

    settle: str | None = None
    if ":" in s:
        s, settle = s.split(":", 1)
        if not settle:
            raise SymbolError(f"bad settle part in {raw!r}")

    base = quote = None
    for sep in _SEPARATORS:
        if sep in s:
            base, quote = s.split(sep, 1)
            break
    if base is None:
        for q in KNOWN_QUOTES:
            if s.endswith(q) and len(s) > len(q):
                base, quote = s[: -len(q)], q
                break
    if not base or not quote:
        raise SymbolError(f"cannot parse symbol {raw!r}")
    if quote not in KNOWN_QUOTES:
        raise SymbolError(f"unknown quote {quote!r} in {raw!r}")
    if settle is not None and settle != quote:
        raise SymbolError(f"settle {settle!r} != quote {quote!r} in {raw!r} (only linear perps)")

    return f"{base}/{quote}:{quote}"


def to_exchange(sym: str) -> str:
    """Канон -> нативный id BingX linear swap: "SOL/USDT:USDT" -> "SOL-USDT"."""
    b, q = base(sym), quote(sym)
    return f"{b}-{q}"


def to_display(sym: str) -> str:
    """Канон -> человекочитаемо для карточек/логов: "SOL/USDT"."""
    return f"{base(sym)}/{quote(sym)}"


def base(sym: str) -> str:
    return _parts(sym)[0]


def quote(sym: str) -> str:
    return _parts(sym)[1]


def _parts(sym: str) -> tuple[str, str]:
    try:
        pair, _settle = sym.split(":", 1)
        b, q = pair.split("/", 1)
    except ValueError as e:
        raise SymbolError(f"not a canonical symbol: {sym!r} (run canonical() first)") from e
    return b, q


def is_canonical(sym: str) -> bool:
    try:
        return canonical(sym) == sym
    except SymbolError:
        return False
