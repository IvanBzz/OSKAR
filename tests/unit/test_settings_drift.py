"""Дрейф конфига (ревью 2026-10-13, данные #10): ключ settings.yaml, который не
читает ни одна строка кода, — ложная ручка. drawdown_pct: 10 обещал safe_mode по
просадке, которого нет; api.port менял порт только на бумаге.

Тест собирает из src/oskar цепочки обращений по строковым ключам —
`x.get("a", {}).get("b")`, `x["a"]["b"]`, `(x or y)["a"]`, через локальные
псевдонимы (`a = x.get("a")` -> `a["b"]`) — и сверяет с листьями settings.yaml.
Задокументированное, но не реализованное — в UNIMPLEMENTED с причиной; второй
тест ловит протухание списка (ключ подключили — убери из списка).
"""

import ast
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SETTINGS = REPO / "config" / "settings.yaml"
SOURCES = REPO / "src" / "oskar"

# Ключ или целая секция -> почему он есть в settings.yaml, но код его не читает.
# В settings.yaml такие ключи помечены «не подключен» / «НЕ РЕАЛИЗОВАН».
UNIMPLEMENTED = {
    "exchange.id": "код только под BingX: фабрики ccxt в data/client.py и адаптер execution/bingx.py",
    "exchange.hedge_mode": "hedge зашит: execution/bingx.py требует positionSide в каждом ордере",
    "exchange.adjust_for_time_difference": "зашит True в make_bingx_exchange / make_bingx_view",
    "exchange.rate_limit_ms": "ccxt enableRateLimit с rateLimit биржи по умолчанию; общего лимитера нет",
    "scanner.universe": "universe = все активные USDT-перпы, зашито в MarketDataClient.universe()",
    "scanner.quick_filter_top": "предранжирования до загрузки klines нет: scan() идет по всему universe",
    "telegram.send_charts": "карточка всегда с графиком, если render_chart удался; выключателя нет",
    "agents.scout.model_cmd": "agents/claude_cli.py зовет `claude -p` сам; команда не настраивается",
    "agents.trader.model_cmd": "то же: команда модели зашита в agents/claude_cli.py",
    "agents.analyst": "агента Analyst еще нет",
    "safe_mode.error_cooldown_minutes": "safe_mode — защелка: автовыхода по кулдауну нет",
    "safe_mode.token_budget_floor_pct": "бюджеты LLM — жесткие daily/hourly лимиты (agents/budget.py)",
    "journal.archive_before_run": "архив журнала только при смене схемы (daemon._connect_journal)",
    "knowledge": "источники индекса зашиты в knowledge/index.py; знания Scout режет max_chars, не токены",
}


def settings_leaves(node: dict, prefix: str = ""):
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            yield from settings_leaves(value, path + ".")
        else:
            yield path


def _key_chain(node) -> tuple[list[str], str | None]:
    """(строковые ключи, имя корневой переменной | None) выражения доступа."""
    keys: list[str] = []
    while True:
        if isinstance(node, ast.BoolOp):  # `(settings or load_settings())["scanner"]`, `x.get("a") or {}`
            node = node.values[0]
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        ):
            keys.append(node.args[0].value)
            node = node.func.value
        elif (
            isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.append(node.slice.value)
            node = node.value
        else:
            break
    return keys[::-1], (node.id if isinstance(node, ast.Name) else None)


def read_paths(root: Path = SOURCES) -> set[str]:
    """Все пути ключей, по которым код что-то читает (с раскрытием локальных псевдонимов)."""
    paths: set[str] = set()
    for file in root.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        chains, aliases = [], {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
                node.targets[0], ast.Name
            ):
                keys, base = _key_chain(node.value)
                if keys:
                    aliases.setdefault(node.targets[0].id, []).append((keys, base))
            keys, base = _key_chain(node)
            if keys:
                chains.append((keys, base))

        def expand(keys, base, depth=0, aliases=aliases):
            yield keys
            if depth < 4:  # `a = a.get(...)` не зациклит
                for parent_keys, parent_base in aliases.get(base, []):
                    for prefix in expand(parent_keys, parent_base, depth + 1):
                        yield prefix + keys

        for keys, base in chains:
            paths.update(".".join(p) for p in expand(keys, base))
    return paths


def _covers(entry: str, path: str) -> bool:
    return path == entry or path.startswith(entry + ".")


def _leaves() -> set[str]:
    return set(settings_leaves(yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))))


def test_every_settings_key_is_read_or_documented():
    dead = sorted(
        p for p in _leaves() - read_paths() if not any(_covers(e, p) for e in UNIMPLEMENTED)
    )
    assert not dead, (
        f"settings.yaml keys that no code reads: {dead}. Wire them, delete them, or add them to "
        "UNIMPLEMENTED with a one-line reason (and mark them in settings.yaml)"
    )


def test_unimplemented_allowlist_is_current():
    leaves, read = _leaves(), read_paths()
    for entry, reason in UNIMPLEMENTED.items():
        covered = {p for p in leaves if _covers(entry, p)}
        assert covered, f"{entry}: no such key in settings.yaml - drop it from UNIMPLEMENTED"
        wired = covered & read
        assert not wired, f"{entry}: now read by code ({wired}) - drop it from UNIMPLEMENTED"
        assert reason.strip() and "\n" not in reason


def test_extractor_sees_reads_of_every_shape():
    """Самопроверка: прямые цепочки, `(x or y)[..]`, псевдонимы — всё видно."""
    read = read_paths()
    for path in (
        "mode", "exchange.sandbox", "detect.swing_length",  # x.get("a", {}).get("b")
        "scanner.full_analysis_top",  # s = (settings or load_settings())["scanner"]; s["..."]
        "agents.scout.daily_budget",  # a = settings.get("agents", {}); a.get("scout", {}).get(...)
        "api.host", "api.port", "journal.db_path", "safe_mode.consecutive_errors",  # подключены 2026-10-13
    ):
        assert path in read, path
