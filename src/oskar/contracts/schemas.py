"""Pydantic-схемы всех сообщений OSKAR (frozen contracts, v1).

Правила (CONSTITUTION.md):
- Ст. 2: LLM выбирает из перечисленных опций по id; свободные числа модели
  в исполнение не попадают. Поэтому TradeDecision содержит только *_option_id.
- Ст. 3 п. 5: confidence ограничен 75 НА УРОВНЕ СХЕМЫ (caps.confidence_cap),
  пока калибровка не докажет право на большее. Поднятие = поправка по ст. 4.
- extra="forbid" везде: неизвестное поле = ошибка парсинга, не тихий мусор.
- Символы ТОЛЬКО в каноническом ccxt-виде: "SOL/USDT:USDT" (см. data/symbols.py).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Direction = Literal["long", "short"]
SetupType = Literal["fvg", "ob", "liquidity_sweep", "bos", "choch", "confluence"]
Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]

CONFIDENCE_CAP = 75  # caps.yaml:confidence_cap; синхронизируется тестом


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------- detectors


class PriceOption(_Contract):
    """Одна опция entry/SL/TP, порожденная детекторами. Trader выбирает по id."""

    id: str
    # kind: market | limit_ob_mid | limit_fvg_edge | swing_low | ob_low | atr_mult |
    #       liquidity_pool | next_fvg | r_multiple
    kind: str
    price: float = Field(gt=0)
    note: str = ""


class FvgInfo(_Contract):
    top: float
    bottom: float
    mitigated: bool
    timeframe: Timeframe


class ObInfo(_Contract):
    top: float
    bottom: float
    tested: bool
    timeframe: Timeframe


class SetupFeatures(_Contract):
    rsi: float | None = None
    atr_pct: float | None = None
    trend_mtf: Literal["bullish", "bearish", "mixed"] | None = None
    fvg: FvgInfo | None = None
    ob: ObInfo | None = None
    swept_liquidity: bool | None = None
    candle_pattern: str | None = None


class SetupCandidate(_Contract):
    """Кандидат сетапа от детерминированного ядра. Вход Scout'а."""

    schema_version: Literal[1] = 1
    id: str
    ts: datetime
    symbol: str  # канонический ccxt: "SOL/USDT:USDT"
    direction: Direction
    setup_type: SetupType
    timeframe: Timeframe
    score: int = Field(ge=0, le=100)
    features: SetupFeatures
    entry_options: list[PriceOption] = Field(min_length=1)
    sl_options: list[PriceOption] = Field(min_length=1)
    tp_options: list[PriceOption] = Field(min_length=1)
    detector_versions: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------ sentinel


class MarketContext(_Contract):
    """Брифинг Sentinel. stale=true деградирует в neutral, НИКОГДА не блокирует
    торговлю (Sentinel — контекст, не альфа; его отсутствие не останавливает цикл)."""

    schema_version: Literal[1] = 1
    ts: datetime
    ttl_sec: int = Field(gt=0)
    regime: Literal["risk_on", "risk_off", "neutral"]
    fear_greed: int | None = Field(default=None, ge=0, le=100)
    btc_dominance: float | None = None
    funding_notes: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    source_health: dict[str, bool] = Field(default_factory=dict)
    stale: bool = False


# --------------------------------------------------------------------- scout


class SetupVerdict(_Contract):
    """Вердикт Scout по одному кандидату. Схема ПРИНУЖДАЕТ к адверсариальности:
    bear_case и bear_rebuttal обязательны и непусты — слабый rebuttal режет
    риск-движок (сжатые дебаты быков/медведей в одном вызове)."""

    candidate_id: str
    bull_case: str = Field(min_length=20)
    bear_case: str = Field(min_length=20)
    bear_rebuttal: str = Field(min_length=20)
    verdict: Literal["take", "pass"]
    confidence: int = Field(ge=0, le=CONFIDENCE_CAP)
    knowledge_refs: list[str] = Field(default_factory=list)


class ScoutReport(_Contract):
    schema_version: Literal[1] = 1
    decision_id: str
    ts: datetime
    setups: list[SetupVerdict]
    model: str
    prompt_sha: str  # реплей офлайн: полный промпт в analysis_log по этому хешу


# -------------------------------------------------------------------- trader


class TradeDecision(_Contract):
    """Trader выбирает опции ПО ID из SetupCandidate. Свободных чисел нет —
    опечатка модели физически не может стать кривым ордером (ст. 2).

    v2: право вето (решение 2026-08-26) — verdict=decline с обязательной
    причиной. Trader = гейткипер исполнения, не только выбор опций."""

    schema_version: Literal[2] = 2
    decision_id: str
    ts: datetime
    candidate_id: str
    verdict: Literal["execute", "decline"]
    entry_option_id: str | None = None
    sl_option_id: str | None = None
    tp_option_id: str | None = None
    rationale: str = Field(min_length=10)
    decline_reason: str | None = None
    model: str
    prompt_sha: str

    @model_validator(mode="after")
    def _verdict_consistency(self) -> "TradeDecision":
        if self.verdict == "execute":
            if not (self.entry_option_id and self.sl_option_id and self.tp_option_id):
                raise ValueError("execute requires entry/sl/tp option ids")
            if self.decline_reason:
                raise ValueError("decline_reason is only valid with verdict=decline")
        else:
            if self.entry_option_id or self.sl_option_id or self.tp_option_id:
                raise ValueError("decline must not carry option ids")
            if not self.decline_reason or len(self.decline_reason) < 20:
                raise ValueError("decline requires decline_reason (>= 20 chars)")
        return self


# ---------------------------------------------------------------------- risk


class RiskStep(_Contract):
    name: str
    passed: bool
    value: str = ""
    limit: str = ""


class RiskComputed(_Contract):
    """Размер/леверидж вычислены ТОЛЬКО Python (ст. 2 п. 3)."""

    qty: float = Field(gt=0)
    leverage: float = Field(ge=1)
    risk_usd: float = Field(ge=0)
    rr_fee_adjusted: float


class RiskVerdict(_Contract):
    schema_version: Literal[1] = 1
    decision_id: str
    ts: datetime
    passed: bool
    steps: list[RiskStep]  # полный след лестницы, включая непройденные шаги
    computed: RiskComputed | None = None
    caps_hash: str  # sha256 caps.yaml на момент вердикта


# ----------------------------------------------------------------- execution


class OrderRef(_Contract):
    exchange_id: str
    client_order_id: str
    type: Literal["MARKET", "LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"]
    status: str


class Fill(_Contract):
    order_id: str
    price: float
    qty: float
    fee: float
    ts: datetime


class ExecutionReport(_Contract):
    schema_version: Literal[1] = 1
    decision_id: str
    ts: datetime
    client_order_id: str  # OSKAR-{run_id}-{decision_id} — ключ реконсиляции (ст. 1)
    orders: list[OrderRef]
    fills: list[Fill] = Field(default_factory=list)
    position_side: Literal["LONG", "SHORT"]  # BingX hedge mode
    reconciled: bool = False


# ------------------------------------------------------------------ learning


class Evidence(_Contract):
    metric: str
    n: int = Field(ge=1)
    value: str


class Lesson(_Contract):
    schema_version: Literal[1] = 1
    id: str
    ts: datetime
    trade_ids: list[str]
    observation: str = Field(min_length=10)
    evidence: Evidence
    proposal_ref: str | None = None
    status: Literal["hypothesis", "validated", "refuted"] = "hypothesis"


class Proposal(_Contract):
    """Предложение самоэволюции: Analyst -> Telegram-approve -> Claudexor ->
    repo-task-proof-loop -> инвариант. risk_class определяет глубину ревью;
    caps идет ТОЛЬКО через процедуру ст. 4."""

    schema_version: Literal[1] = 1
    id: str
    ts: datetime
    title: str
    problem: str
    evidence: Evidence
    proposed_change: str
    declared_invariant: str  # обязателен ДО реализации (ст. 5)
    risk_class: Literal["prompt", "knowledge", "detector", "risk", "execution", "caps"]
    status: Literal["draft", "approved", "implemented", "validated", "reverted"] = "draft"
