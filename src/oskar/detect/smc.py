"""Обертка smartmoneyconcepts с защитой от look-ahead.

САМЫЙ ОПАСНЫЙ ТИХИЙ БАГ ДЕТЕКТ-СЛОЯ живет здесь, поэтому правила жесткие:

1. analyze() принимает ТОЛЬКО закрытые свечи и видит ТОЛЬКО префикс истории.
   Реплей обязан вызывать analyze(candles[:t]) на каждом баре — никакого
   расчета по всей серии с последующей фильтрацией.
2. Свинги подтверждаются swing_length барами БУДУЩЕГО (библиотека: shift(-N)
   внутри rolling). Хвостовые swing_length баров — неподтвержденная зона,
   свинги там маскируются. Библиотека вдобавок ПРИНУДИТЕЛЬНО помечает первый
   и последний бар свингами (для альтернации) — сфабрикованные метки, обе
   умирают под маской (последний — в хвосте, первый — явным NaN).
3. Семантика времени события: BOS/CHoCH «известен» на свече пробоя
   (broken_index), OB — на свече креации (пробой свинга), не на баре разметки.
   FVG размечен на средней свече i и известен с закрытия i+1 (на префиксе
   последний бар физически не может нести FVG — shift(-1)).
4. Прочие quirks библиотеки: MitigatedIndex/Swept == 0 означает «еще не
   произошло в пределах данных»; pip_range ликвидности зависит от глобального
   диапазона переданных данных (на разных префиксах чуть плавает — допустимо,
   префикс-детерминизм сохраняется).

Известное свойство (не баг): структура «перерисовывается» — подтвержденный
свинг может быть позже вытеснен более экстремальным без промежуточного
отката (дедупликация одноименных свингов). Live-решение в момент t опирается
на то, что было известно в t, — реплей воспроизводит это честно, потому что
пересчитывает каждый префикс заново.
"""

import contextlib
import io
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Библиотека печатает баннер с эмодзи при импорте: под cp1251-stdout (Windows,
# редирект в файл) это UnicodeEncodeError и падение демона на старте. Глушим.
with contextlib.redirect_stdout(io.StringIO()):
    from smartmoneyconcepts import smc

DETECTOR_VERSION = f"smartmoneyconcepts=={smc.__version__}+oskar.smc=1"


@dataclass(frozen=True)
class SmcConfig:
    swing_length: int = 10
    join_consecutive_fvg: bool = True
    liquidity_range_percent: float = 0.01
    structure_close_break: bool = True


@dataclass(frozen=True)
class Swing:
    index: int
    kind: str  # high | low
    level: float


@dataclass(frozen=True)
class FvgZone:
    index: int  # средняя свеча формации
    direction: str  # bullish | bearish
    top: float
    bottom: float
    mitigated: bool  # в пределах префикса


@dataclass(frozen=True)
class OrderBlock:
    index: int
    direction: str
    top: float
    bottom: float
    volume: float
    strength_pct: float
    mitigated: bool


@dataclass(frozen=True)
class StructureEvent:
    kind: str  # bos | choch
    direction: str
    swing_index: int
    level: float
    broken_index: int  # МОМЕНТ, когда событие стало известно


@dataclass(frozen=True)
class LiquidityPool:
    index: int
    direction: str  # bullish (equal highs) | bearish (equal lows)
    level: float
    end_index: int
    swept: bool
    swept_index: int | None


@dataclass(frozen=True)
class SmcSnapshot:
    bar_index: int  # индекс последней закрытой свечи (t)
    detector_version: str
    swings: list[Swing] = field(default_factory=list)
    fvgs: list[FvgZone] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    structure: list[StructureEvent] = field(default_factory=list)
    liquidity: list[LiquidityPool] = field(default_factory=list)

    def active_fvgs(self) -> list[FvgZone]:
        return [z for z in self.fvgs if not z.mitigated]

    def active_order_blocks(self) -> list[OrderBlock]:
        return [b for b in self.order_blocks if not b.mitigated]

    def intact_liquidity(self) -> list[LiquidityPool]:
        return [p for p in self.liquidity if not p.swept]


MIN_BARS_FACTOR = 3  # минимум данных: swing_length * 3

DEFAULT_CONFIG = SmcConfig()


def _prepare(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Библиотека использует позиционные срезы — обязателен RangeIndex."""
    df = ohlc.rename(columns={c: c.lower() for c in ohlc.columns})
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"ohlc missing columns: {sorted(missing)}")
    return df[["open", "high", "low", "close", "volume"]].reset_index(drop=True)


def confirmed_swings(ohlc: pd.DataFrame, swing_length: int) -> pd.DataFrame:
    """Свинги с маской неподтвержденной зоны: хвост swing_length баров + бар 0."""
    raw = smc.swing_highs_lows(ohlc, swing_length=swing_length)
    cutoff = len(ohlc) - swing_length
    masked = raw.copy()
    masked.loc[masked.index >= cutoff, ["HighLow", "Level"]] = np.nan
    masked.loc[0, ["HighLow", "Level"]] = np.nan  # принудительная метка библиотеки
    return masked


def analyze(ohlc: pd.DataFrame, config: SmcConfig = DEFAULT_CONFIG) -> SmcSnapshot:
    """Полный SMC-снапшот по префиксу закрытых свечей."""
    df = _prepare(ohlc)
    n = len(df)
    if n < config.swing_length * MIN_BARS_FACTOR:
        return SmcSnapshot(bar_index=n - 1, detector_version=DETECTOR_VERSION)

    swings_df = confirmed_swings(df, config.swing_length)

    swings = [
        Swing(index=int(i), kind="high" if hl == 1 else "low", level=float(lv))
        for i, hl, lv in zip(
            swings_df.index, swings_df["HighLow"], swings_df["Level"], strict=True
        )
        if not np.isnan(hl)
    ]

    fvg_df = smc.fvg(df, join_consecutive=config.join_consecutive_fvg)
    fvgs = [
        FvgZone(
            index=int(i),
            direction="bullish" if row.FVG == 1 else "bearish",
            top=float(row.Top),
            bottom=float(row.Bottom),
            mitigated=row.MitigatedIndex > 0,
        )
        for i, row in fvg_df.iterrows()
        if not np.isnan(row.FVG)
    ]

    ob_df = smc.ob(df, swings_df)
    order_blocks = [
        OrderBlock(
            index=int(i),
            direction="bullish" if row.OB == 1 else "bearish",
            top=float(row.Top),
            bottom=float(row.Bottom),
            volume=float(row.OBVolume),
            strength_pct=float(row.Percentage),
            mitigated=row.MitigatedIndex > 0,
        )
        for i, row in ob_df.iterrows()
        if not np.isnan(row.OB)
    ]

    st_df = smc.bos_choch(df, swings_df, close_break=config.structure_close_break)
    structure: list[StructureEvent] = []
    for i, row in st_df.iterrows():
        for kind, val in (("bos", row.BOS), ("choch", row.CHOCH)):
            if not np.isnan(val):
                structure.append(
                    StructureEvent(
                        kind=kind,
                        direction="bullish" if val == 1 else "bearish",
                        swing_index=int(i),
                        level=float(row.Level),
                        broken_index=int(row.BrokenIndex),
                    )
                )

    liq_df = smc.liquidity(df, swings_df, range_percent=config.liquidity_range_percent)
    liquidity = [
        LiquidityPool(
            index=int(i),
            direction="bullish" if row.Liquidity == 1 else "bearish",
            level=float(row.Level),
            end_index=int(row.End),
            swept=row.Swept > 0,
            swept_index=int(row.Swept) if row.Swept > 0 else None,
        )
        for i, row in liq_df.iterrows()
        if not np.isnan(row.Liquidity)
    ]

    return SmcSnapshot(
        bar_index=n - 1,
        detector_version=DETECTOR_VERSION,
        swings=swings,
        fvgs=fvgs,
        order_blocks=order_blocks,
        structure=structure,
        liquidity=liquidity,
    )
