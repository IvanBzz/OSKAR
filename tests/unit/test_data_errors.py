"""Дисциплина ошибок клиента данных (урок бана 2026-10-11).

Живые факты: 109415 «<symbol> is pause currently» на токенизированных активах
-> >10 за 15 мин -> 109429 «over 10 error code:109415 ... can retry after time:
<epoch ms>» — бан kline-эндпоинта, который старый клиент принимал за вину
символов и хоронил здоровые ON/NIL.
"""

import pytest

from oskar.data.client import (
    DEAD_TTL_SEC,
    ERROR_BUDGET,
    ERROR_WINDOW_SEC,
    ExchangeCooldown,
    MarketDataClient,
    classify_error,
    parse_retry_after_ms,
)


def ban_text(retry_ms) -> str:
    """Дословный ответ BingX 2026-10-11 (бан kline-эндпоинта)."""
    return (
        'bingx {"code":109429,"msg":"over 10 error code:109415 requests within 900000 ms '
        "for this api, please verify and fix it ,can retry after time: "
        + str(retry_ms) + '","data":{}}'
    )


def paused_text(sym) -> str:
    """Дословный ответ BingX на паузный (RWA) символ."""
    return (
        'bingx {"code":109415,"msg":"' + sym + " is pause currently,all validted symbols in "
        'api:/openApi/swap/v2/quote/contracts, please verify it","data":{}}'
    )


TRANSIENT_TEXT = 'bingx {"code":100410,"msg":"请稍后重试"}'


class ExchangeError(Exception):
    """Имитация ccxt.ExchangeError."""


class OperationFailed(Exception):
    """Имитация ccxt.OperationFailed (100410 приходит этим типом)."""


def make_raw(n: int):
    return [[1_700_000_000_000 + i * 900_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000] for i in range(n)]


class ScriptedExchange:
    """Для каждого символа — очередь ответов: исключение или список свечей."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        self.calls.append(symbol)
        queue = self.script.get(symbol)
        if not queue:
            return make_raw(limit or 101)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def load_markets(self, reload=False):
        return {}


WALL0 = 1_791_714_000.0  # произвольный «настоящий» момент (сек)


def make_client(ex, t):
    return MarketDataClient(ex, clock=lambda: t[0], wall=lambda: WALL0, sleep=lambda s: None)


# ---------------------------------------------------------------- классификация


def test_classify_by_bingx_code():
    assert classify_error(ExchangeError(ban_text(1))) == "cooldown"
    assert classify_error(ExchangeError(paused_text("X-USDT"))) == "symbol"
    assert classify_error(OperationFailed(TRANSIENT_TEXT)) == "transient"
    assert classify_error(ConnectionError("reset")) == "network"
    assert classify_error(ExchangeError("bingx 109415 without json")) == "symbol"


def test_parse_retry_after_epoch_ms():
    assert parse_retry_after_ms(ExchangeError(ban_text(1791714015977))) == 1791714015977
    assert parse_retry_after_ms(ExchangeError("no hint")) is None


# ------------------------------------------------------------------- 109429


def test_ban_enters_cooldown_and_spares_symbol():
    """Бан — глобальная пауза до retry-after биржи; символ НЕ в черном списке,
    остальные символы до биржи не ходят, после паузы — ходят."""
    t = [1000.0]
    retry_ms = int((WALL0 + 600) * 1000)  # биржа: повторить через 10 минут
    ex = ScriptedExchange({"ON/USDT:USDT": [ExchangeError(ban_text(retry_ms))]})
    client = make_client(ex, t)

    with pytest.raises(ExchangeError):
        client.fetch_closed_ohlcv("ON/USDT:USDT", "15m", 100)
    assert "ON/USDT:USDT" not in client.dead_symbols
    assert 600 <= client.cooldown_remaining() <= 610

    with pytest.raises(ExchangeCooldown):
        client.fetch_closed_ohlcv("NIL/USDT:USDT", "15m", 100)
    assert ex.calls == ["ON/USDT:USDT"]  # ни одного запроса во время бана

    t[0] += 611
    assert client.cooldown_remaining() == 0
    df = client.fetch_closed_ohlcv("NIL/USDT:USDT", "15m", 100)
    assert len(df) == 100 and ex.calls[-1] == "NIL/USDT:USDT"
    df = client.fetch_closed_ohlcv("ON/USDT:USDT", "15m", 100)  # и «забаненный» жив
    assert len(df) == 100


def test_ban_without_retry_hint_uses_default_cooldown():
    t = [0.0]
    ex = ScriptedExchange({"A/USDT:USDT": [ExchangeError('bingx {"code":109429,"msg":"restricted"}')]})
    client = make_client(ex, t)
    with pytest.raises(ExchangeError):
        client.fetch_closed_ohlcv("A/USDT:USDT", "15m", 100)
    assert 900 <= client.cooldown_remaining() <= 910


# ------------------------------------------------------------------- 109415


def test_paused_symbol_blacklisted_with_ttl():
    t = [0.0]
    ex = ScriptedExchange({"NCSKX2USD/USDT:USDT": [ExchangeError(paused_text("NCSKX2USD-USDT"))]})
    client = make_client(ex, t)
    with pytest.raises(ExchangeError):
        client.fetch_closed_ohlcv("NCSKX2USD/USDT:USDT", "15m", 100)
    with pytest.raises(RuntimeError, match="blacklisted"):
        client.fetch_closed_ohlcv("NCSKX2USD/USDT:USDT", "15m", 100)
    assert len(ex.calls) == 1
    assert client.cooldown_remaining() == 0  # одна ошибка бюджет не исчерпывает

    t[0] += DEAD_TTL_SEC + 1  # приговор истек — символ переоценивается
    df = client.fetch_closed_ohlcv("NCSKX2USD/USDT:USDT", "15m", 100)
    assert len(df) == 100 and len(ex.calls) == 2


def test_error_budget_triggers_voluntary_cooldown():
    """ERROR_BUDGET ошибок класса «символ» за окно -> сами уходим в паузу до
    выхода самой старой из окна; биржевых 10 не достигаем никогда."""
    t = [0.0]
    syms = [f"D{i}/USDT:USDT" for i in range(ERROR_BUDGET + 2)]
    ex = ScriptedExchange({s: [ExchangeError(paused_text(s))] for s in syms})
    client = make_client(ex, t)

    for i, s in enumerate(syms[:ERROR_BUDGET]):
        t[0] = 10.0 * i  # ошибки идут раз в 10 секунд
        with pytest.raises(ExchangeError):
            client.fetch_closed_ohlcv(s, "15m", 100)
    assert client.errors_in_window() == ERROR_BUDGET
    assert client.cooldown_remaining() > 0
    assert "error budget" in client.cooldown_reason

    with pytest.raises(ExchangeCooldown):
        client.fetch_closed_ohlcv(syms[ERROR_BUDGET], "15m", 100)
    assert len(ex.calls) == ERROR_BUDGET  # лишний запрос до биржи не дошел

    # окно биржи (900с от первой ошибки) прошло — поток возобновляется
    t[0] = ERROR_WINDOW_SEC + 10
    assert client.cooldown_remaining() == 0
    with pytest.raises(ExchangeError):
        client.fetch_closed_ohlcv(syms[ERROR_BUDGET], "15m", 100)
    assert len(ex.calls) == ERROR_BUDGET + 1


# ------------------------------------------------------------------- 100410


def test_transient_100410_retried_and_not_blacklisted():
    t = [0.0]
    ex = ScriptedExchange({"BOME/USDT:USDT": [OperationFailed(TRANSIENT_TEXT)]})  # 1 сбой, потом свечи
    client = make_client(ex, t)
    df = client.fetch_closed_ohlcv("BOME/USDT:USDT", "15m", 100)
    assert len(df) == 100
    assert ex.calls == ["BOME/USDT:USDT", "BOME/USDT:USDT"]
    assert "BOME/USDT:USDT" not in client.dead_symbols
    assert client.errors_in_window() == 0


# ------------------------------------------------------------------- universe


def _market(symbol, *, status=1, active=True, base=None):
    b = base or symbol.split("/")[0]
    return {
        "symbol": symbol, "base": b, "swap": True, "active": active,
        "quote": "USDT", "settle": "USDT",
        "info": {"status": status, "apiStateOpen": "true", "displayName": f"{b}-USDT"},
    }


def test_universe_excludes_paused_and_tokenized_assets():
    """status 25 (пауза) и RWA-перпы (NCSK/NCFX/NCCO/NCSI ... 2USD) — вне юниверса,
    даже с active=True (ccxt active смотрит на apiStateOpen и паузы не видит)."""

    class Ex(ScriptedExchange):
        def load_markets(self, reload=False):
            return {
                "BTC/USDT:USDT": _market("BTC/USDT:USDT"),
                "NCT/USDT:USDT": _market("NCT/USDT:USDT"),  # настоящая крипта на NC — остается
                "NCFXEUR2JPY/USDT:USDT": _market("NCFXEUR2JPY/USDT:USDT", status=25),
                "NCCOGOLD2USD/USDT:USDT": _market("NCCOGOLD2USD/USDT:USDT", status=1),  # RWA, хоть и online
                "NCSKAAPL2USD/USDT:USDT": _market("NCSKAAPL2USD/USDT:USDT", status=25),
                "NCSKAXONUSDT/USDT:USDT": _market("NCSKAXONUSDT/USDT:USDT", status=1),  # RWA без 2USD
                "PRE/USDT:USDT": _market("PRE/USDT:USDT", status=5),  # pre-online
                "OLD/USDT:USDT": _market("OLD/USDT:USDT", active=False),
                "NOINFO/USDT:USDT": {"symbol": "NOINFO/USDT:USDT", "swap": True, "active": True,
                                     "quote": "USDT", "settle": "USDT"},
            }

    client = make_client(Ex({}), [0.0])
    assert client.universe() == ["BTC/USDT:USDT", "NCT/USDT:USDT", "NOINFO/USDT:USDT"]
    assert client.universe_excluded == {"status": 3, "rwa": 2}
    health = client.health()
    assert health["universe"] == 3 and health["universe_excluded"] == {"status": 3, "rwa": 2}
    assert health["cooldown_sec"] == 0 and health["dead_symbols"] == 0


def test_universe_can_keep_rwa_when_asked():
    class Ex(ScriptedExchange):
        def load_markets(self, reload=False):
            return {"NCCOGOLD2USD/USDT:USDT": _market("NCCOGOLD2USD/USDT:USDT", status=1)}

    client = MarketDataClient(Ex({}), clock=lambda: 0.0, sleep=lambda s: None, include_rwa=True)
    assert client.universe() == ["NCCOGOLD2USD/USDT:USDT"]
