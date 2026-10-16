"""Клиент рыночных данных — обертка ccxt BingX.

Дисциплина ЗАКРЫТЫХ свечей: ccxt возвращает последнюю, еще ФОРМИРУЮЩУЮСЯ
свечу последней строкой — она всегда отбрасывается. Детект-слой видит только
закрытые бары (look-ahead защита начинается уже здесь).

Биржевой объект инжектится (тесты — фейк, прод — фабрика make_bingx_exchange).
Символы: на вход канонические ("SOL/USDT:USDT") — ccxt unified принимает их
напрямую, конвертация не нужна.

Дисциплина ошибок (урок бана 2026-10-11, см. changelog):
- 109415 «<symbol> is pause currently» — символ на паузе (токенизированные
  акции/форекс/сырье вне торговых часов). BingX считает ТАКИЕ ошибки: >10 за
  900с -> 109429, бан kline-эндпоинта на 15 мин. Поэтому паузные символы
  режутся еще на уровне universe (status != 1), а каждая ошибка класса
  «символ» идет в черный список с TTL и в бюджет ошибок.
- 109429 — ГЛОБАЛЬНЫЙ бан эндпоинта, не вина символа: клиент уходит в
  cooldown до времени из ответа биржи и не трогает сеть; черный список
  НЕ пополняется (ранее бан хоронил здоровые ON/NIL — класс бага «журнал врет»).
- 100410 «请稍后重试» (попробуйте позже) — преходящая, ретрай без приговора.
- Бюджет ошибок: ERROR_BUDGET ошибок класса «символ» за окно -> добровольный
  cooldown до выхода самой старой из окна. Биржевого порога не достигаем никогда.
"""

import re
import time
from collections import Counter, deque
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import pandas as pd

from oskar.data.symbols import canonical

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
RETRIES = 2
RETRY_DELAY_SEC = 1.0
UNIVERSE_TTL_SEC = 4 * 3600  # v1: кеш списка пар 4 часа
DEAD_TTL_SEC = 4 * 3600  # мертвый символ переоценивается вместе с universe
ERROR_WINDOW_SEC = 900.0  # окно BingX: «over 10 error code:109415 requests within 900000 ms»
ERROR_BUDGET = 6  # наш потолок — заметно ниже биржевого (10)
DEFAULT_COOLDOWN_SEC = 900.0  # если retry-after из ответа не распарсился
RATE_LIMIT_COOLDOWN_SEC = 60.0
COOLDOWN_MARGIN_SEC = 5.0

COOLDOWN_CODES = frozenset({"109429"})
TRANSIENT_CODES = frozenset({"100410"})
# Токенизированные активы BingX (RWA-перпы): NCSK акции, NCFX форекс, NCCO сырье,
# NCSI индексы — base вида NCSKAAPL2USD / NCFXEUR2JPY / NCSKAXONUSDT. Торгуются
# сессиями, вне часов стоят на паузе (status 25) и отвечают 109415. Стратегия — крипта.
RWA_BASE_RE = re.compile(r"^NC(SK|FX|CO|SI)[A-Z0-9]{2,}$")

_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d+)"?')
_RETRY_AFTER_RE = re.compile(r"retry after time:\s*(\d{10,13})")
# HTTP 429 — только как статус ответа, не любое «429» в тексте: в тексте ccxt бывают URL с
# параметрами (quantity=429), цены (0.429) и код 109429 (ревью диффа Codex, оба раунда)
_HTTP_429_STATUS_RE = re.compile(r"\b429\s+Too Many Requests\b", re.I)
_HTTP_429_RE = re.compile(r'HTTP(?:/\d(?:\.\d)?)?\s*429\b|"code"\s*:\s*"?429\b')
_RETRY_AFTER_HEADER_RE = re.compile(r"\bretry-after\b[\"']?\s*[:=]\s*[\"']?([^\r\n\"'}]+)", re.I)


class ExchangeLike(Protocol):
    """Сигнатура как у ccxt: третий ПОЗИЦИОННЫЙ аргумент — since, не limit!
    (баг с limit на месте since дал since=201мс-от-1970 -> пустые ответы,
    ошибки 109415 и 15-минутный бан API — limit передаем ТОЛЬКО как keyword)."""

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m",
        since: int | None = None, limit: int | None = None,
    ) -> list[list]: ...

    def load_markets(self, reload: bool = False) -> dict[str, Any]: ...


class ExchangeCooldown(RuntimeError):
    """Kline-эндпоинт на паузе (rate limit, бан 109429 или бюджет ошибок): до биржи не ходим."""

    def __init__(self, remaining_sec: float, reason: str):
        self.remaining_sec = remaining_sec
        self.reason = reason
        super().__init__(f"kline API cooldown: {remaining_sec:.0f}s left ({reason})")


def _retryable(e: Exception) -> bool:
    """Сетевые сбои — ретраим всегда."""
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    retryable_names = {"NetworkError", "RequestTimeout", "ExchangeNotAvailable"}
    return any(base.__name__ in retryable_names for base in type(e).__mro__)


def exchange_error_code(e: Exception) -> str | None:
    """Код BingX из текста ccxt-исключения: bingx {"code":109415,...}."""
    m = _CODE_RE.search(str(e))
    return m.group(1) if m else None


def classify_error(e: Exception) -> str:
    """network | cooldown | transient | symbol.

    Только класс «symbol» приговаривает символ и расходует бюджет ошибок:
    именно эти ответы BingX считает в счетчике нарушений."""
    code = exchange_error_code(e)
    text = str(e)
    # Ревью 2026-10-13, #25: лимит общий для клиента; ccxt наследует его от сетевых ошибок.
    if (any(base.__name__ in {"DDoSProtection", "RateLimitExceeded"} for base in type(e).__mro__)
            or _HTTP_429_STATUS_RE.search(text)):
        return "cooldown"
    # Сетевую ошибку решает ТИП, а не текст: в тексте ccxt — URL запроса с параметрами,
    # и «quantity=429» превращал таймаут входа в «rate limit» -> исполнитель повторял
    # вход вслепую (ревью Codex раунд 2, high #1)
    if _retryable(e):
        return "network"
    if code in COOLDOWN_CODES or code == "429" or _HTTP_429_RE.search(text):
        return "cooldown"
    if code in TRANSIENT_CODES:
        return "transient"
    # ccxt типизирует серверные сбои (100500 и т.п.) как OperationFailed — это
    # не вина символа независимо от кода (ревью 2026-10-13: 6 ответов 5xx за скан
    # хоронили 6 здоровых символов и уводили в 15-минутную паузу)
    if any(base.__name__ == "OperationFailed" for base in type(e).__mro__):
        return "transient"
    return "symbol"


def parse_retry_after_ms(e: Exception) -> int | None:
    """«can retry after time: 1791714015977» -> epoch ms."""
    m = _RETRY_AFTER_RE.search(str(e))
    if m is None:
        return None
    raw = m.group(1)
    return int(raw) if len(raw) == 13 else int(raw) * 1000


def rate_limit_wait(e: Exception, wall: float) -> float:
    """Ревью 2026-10-13, #25: уважаем Retry-After (секунды/HTTP-date), иначе ждем 60 с."""
    match = _RETRY_AFTER_HEADER_RE.search(str(e))
    if match is not None:
        value = match.group(1).strip()
        seconds = re.match(r"\d+(?:\.\d+)?(?:\s|[,;]|$)", value)
        if seconds is not None:
            return float(seconds.group().rstrip(",; "))
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - wall)
        except (ValueError, TypeError, OverflowError):
            pass
    return RATE_LIMIT_COOLDOWN_SEC


class MarketDataClient:
    def __init__(
        self,
        exchange: ExchangeLike,
        *,
        clock=time.monotonic,
        wall=time.time,
        sleep=time.sleep,
        include_rwa: bool = False,
    ):
        self._ex = exchange
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._include_rwa = include_rwa
        self._universe: list[str] | None = None
        self._universe_at: float = -UNIVERSE_TTL_SEC
        self.universe_excluded: Counter = Counter()  # причина -> сколько символов отрезано
        # symbol -> monotonic-момент истечения приговора. Только ошибки класса
        # «symbol» (мертвый/паузный листинг); бан и сеть символ не хоронят.
        self.dead_symbols: dict[str, float] = {}
        self._symbol_errors: deque[float] = deque()  # моменты ошибок класса «symbol» (окно 15 мин)
        self._cooldown_until: float = -1.0
        self.cooldown_reason: str = ""

    # --------------------------------------------------------------- health

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._clock())

    def errors_in_window(self) -> int:
        self._prune_errors(self._clock())
        return len(self._symbol_errors)

    def health(self) -> dict:
        """Срез для /status: видно, почему вотчлист пуст или данные стоят.
        Только чтение: вызывается из потока API параллельно сканеру."""
        now = self._clock()
        alive_dead = sum(1 for until in list(self.dead_symbols.values()) if until > now)
        remaining = self.cooldown_remaining()
        return {
            "universe": len(self._universe or []),
            "universe_excluded": dict(self.universe_excluded),
            "dead_symbols": alive_dead,
            "errors_15m": self.errors_in_window(),
            "cooldown_sec": int(remaining),
            "cooldown_reason": self.cooldown_reason if remaining > 0 else "",
        }

    # ------------------------------------------------------------ internals

    def _prune_errors(self, now: float) -> None:
        while self._symbol_errors and now - self._symbol_errors[0] > ERROR_WINDOW_SEC:
            self._symbol_errors.popleft()

    def _purge_dead(self, now: float) -> None:
        for sym in [s for s, until in self.dead_symbols.items() if until <= now]:
            del self.dead_symbols[sym]

    def _enter_cooldown(self, until: float, reason: str) -> None:
        if until > self._cooldown_until:
            self._cooldown_until = until
            self.cooldown_reason = reason

    def _check_cooldown(self) -> None:
        remaining = self.cooldown_remaining()
        if remaining > 0:
            raise ExchangeCooldown(remaining, self.cooldown_reason)

    def _on_exchange_error(self, sym: str, e: Exception, now: float) -> None:
        kind = classify_error(e)
        if kind == "cooldown":
            if exchange_error_code(e) not in COOLDOWN_CODES:
                self._enter_cooldown(now + rate_limit_wait(e, self._wall()), "exchange rate limit")
                return
            retry_ms = parse_retry_after_ms(e)
            if retry_ms is None:
                wait = DEFAULT_COOLDOWN_SEC
            else:
                wait = max(0.0, retry_ms / 1000 - self._wall())
            self._enter_cooldown(
                now + wait + COOLDOWN_MARGIN_SEC, f"exchange ban {exchange_error_code(e)}"
            )
        elif kind == "symbol":
            self.dead_symbols[sym] = now + DEAD_TTL_SEC
            self._symbol_errors.append(now)
            self._prune_errors(now)
            if len(self._symbol_errors) >= ERROR_BUDGET:
                # пауза до выхода самой старой ошибки из окна биржи: счетчик
                # нарушений никогда не доползает до биржевых 10
                until = self._symbol_errors[0] + ERROR_WINDOW_SEC + COOLDOWN_MARGIN_SEC
                self._enter_cooldown(
                    until, f"error budget {len(self._symbol_errors)}/{ERROR_BUDGET} per 15m"
                )
        # network / transient: символ жив, ничего не копим

    def _with_retry(self, fn, *args, **kwargs):
        last: Exception | None = None
        for attempt in range(1 + RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - фильтруем через classify_error
                last = e
                # Ревью 2026-10-13, #25: cooldown не повторяем даже для текущего символа.
                if classify_error(e) not in ("network", "transient") or attempt >= RETRIES:
                    raise
                self._sleep(RETRY_DELAY_SEC * (attempt + 1))
        raise last  # type: ignore[misc]

    # ---------------------------------------------------------------- public

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        """OHLCV ТОЛЬКО закрытых свечей: последняя (формирующаяся) отброшена.

        Запрашивается limit+1, чтобы после отброса осталось ровно limit.
        Возвращает DataFrame [open, high, low, close, volume] c RangeIndex
        и колонкой ts (миллисекунды открытия бара).
        """
        sym = canonical(symbol)
        now = self._clock()
        self._check_cooldown()
        self._purge_dead(now)
        if sym in self.dead_symbols:
            raise RuntimeError(f"{sym} is blacklisted (prior exchange error on klines)")
        try:
            raw = self._with_retry(self._ex.fetch_ohlcv, sym, timeframe, limit=limit + 1)
        except Exception as e:  # noqa: BLE001
            self._on_exchange_error(sym, e, self._clock())
            if classify_error(e) == "cooldown" and exchange_error_code(e) not in COOLDOWN_CODES:
                # Скан прерывается сразу, даже если остальные символы есть в кеше (ревью #25).
                raise ExchangeCooldown(self.cooldown_remaining(), self.cooldown_reason) from e
            raise
        if not raw or len(raw) < 2:
            raise RuntimeError(f"not enough candles for {sym} {timeframe}: got {len(raw or [])}")
        df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
        df = df.iloc[:-1].reset_index(drop=True)  # ФОРМИРУЮЩАЯСЯ свеча отброшена
        return df.astype(
            {"open": float, "high": float, "low": float, "close": float, "volume": float}
        )

    def last_price(self, symbol: str) -> float:
        """Последняя цена по тикеру (другой эндпоинт — бан klines его не касается).
        Нужна исполнению: проверить, что план не устарел между карточкой и кнопкой."""
        ticker = self._with_retry(self._ex.fetch_ticker, canonical(symbol))
        px = ticker.get("last") or ticker.get("close")
        if not px:
            raise RuntimeError(f"no last price for {symbol}")
        return float(px)

    def _exclusion_reason(self, m: dict) -> str | None:
        info = m.get("info") or {}
        status = info.get("status")
        # BingX status: 1 online; 25 пауза/запрет открытия; 5 pre-online; 0 offline.
        # ccxt active смотрит на apiStateOpen и паузу НЕ видит.
        if status is not None and str(status) != "1":
            return "status"
        base = m.get("base") or str(m.get("symbol", "")).split("/")[0]
        if not self._include_rwa and RWA_BASE_RE.match(base):
            return "rwa"
        return None

    def universe(self) -> list[str]:
        """Активные торгуемые USDT-перпы (крипта, status=1), канонические символы. Кеш 4 часа."""
        now = self._clock()
        if self._universe is not None and now - self._universe_at < UNIVERSE_TTL_SEC:
            return self._universe
        markets = self._with_retry(self._ex.load_markets, True)
        out = []
        excluded: Counter = Counter()
        for m in markets.values():
            if not (
                m.get("swap")
                and m.get("active")
                and m.get("quote") == "USDT"
                and m.get("settle") == "USDT"
            ):
                continue
            reason = self._exclusion_reason(m)
            if reason is not None:
                excluded[reason] += 1
                continue
            out.append(canonical(m["symbol"]))
        self._universe = sorted(set(out))
        self._universe_at = now
        self.universe_excluded = excluded
        return self._universe


def make_bingx_exchange(api_key: str = "", secret: str = "", sandbox: bool = True):
    """Фабрика для ПРИВАТНЫХ вызовов (баланс/ордера): только VST до гейта Фазы 5.
    Блок H, 2026-10-16: sandbox — именно True, иначе исключение (было `if sandbox:` —
    False/None молча давали реальный хост)."""
    import ccxt

    from oskar.core.env_gate import require_vst

    require_vst(sandbox)
    ex = ccxt.bingx({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        # defaultType=swap ОБЯЗАТЕЛЕН: без него fetch_balance уходит в SPOT
        # реального аккаунта даже в sandbox (VST — это swap-хост)
        "options": {"adjustForTimeDifference": True, "defaultType": "swap"},
    })
    ex.set_sandbox_mode(True)
    return ex


def make_bingx_public_exchange():
    """Фабрика для РЫНОЧНЫХ ДАННЫХ: продакшн-хост, без sandbox и без ключей.

    Свечи/markets публичны, VST-цены == реальные цены (в этом суть VST).
    Kline-эндпоинт банит за ошибки (109429 после >10 x 109415 за 15 мин) —
    рыночные данные ходят мимо VST-хоста всегда, а ошибки учитывает клиент.
    """
    import ccxt

    return ccxt.bingx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
