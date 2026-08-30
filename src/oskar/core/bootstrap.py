"""Сборка рантайма демона из конфигов (caps.yaml / profiles.yaml / settings.yaml).

Единственное место, где YAML превращается в типизированные объекты ядра.
Капсы читаются ПОСЛЕ verify_caps() демона — тут только парсинг.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from oskar.core.cycle import CycleConfig
from oskar.detect.smc import SmcConfig
from oskar.risk.ladder import Caps, ProfileLimits

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def load_caps() -> Caps:
    raw = _load("caps.yaml")
    return Caps(
        risk_per_trade_pct=float(raw["risk_per_trade_pct"]),
        max_total_risk_pct=float(raw["max_total_risk_pct"]),
        max_leverage=float(raw["max_leverage"]),
        confidence_cap=int(raw["confidence_cap"]),
        min_rr_fee_adjusted=float(raw["min_rr_fee_adjusted"]),
        max_open_positions=int(raw["max_open_positions"]),
    )


@dataclass(frozen=True)
class ProfileSettings:
    name: str
    limits: ProfileLimits
    signal_timeframes: tuple[str, ...]
    context_timeframes: tuple[str, ...]
    approval_ttl_seconds: int
    scan_interval_seconds: int


def load_active_profile() -> ProfileSettings:
    raw = _load("profiles.yaml")
    name = raw["active_profile"]
    p = raw["profiles"][name]
    return ProfileSettings(
        name=name,
        limits=ProfileLimits(
            sl_dist_pct_min=float(p["sl_distance_pct"][0]),
            sl_dist_pct_max=float(p["sl_distance_pct"][1]),
            max_leverage=float(p["max_leverage"]),
        ),
        signal_timeframes=tuple(p["signal_timeframes"]),
        context_timeframes=tuple(p["context_timeframes"]),
        approval_ttl_seconds=int(p["approval_ttl_seconds"]),
        scan_interval_seconds=int(p["scan_interval_seconds"]),
    )


@dataclass(frozen=True)
class ScannerSettings:
    full_analysis_top: int
    min_opportunity_score: int
    min_hourly_quote_volume_usd: float
    per_symbol_cooldown_seconds: int


def load_settings() -> dict:
    return _load("settings.yaml")


def load_scanner_settings(settings: dict | None = None) -> ScannerSettings:
    s = (settings or load_settings())["scanner"]
    return ScannerSettings(
        full_analysis_top=int(s["full_analysis_top"]),
        min_opportunity_score=int(s["min_opportunity_score"]),
        min_hourly_quote_volume_usd=float(s["min_hourly_quote_volume_usd"]),
        per_symbol_cooldown_seconds=int(s["per_symbol_cooldown_seconds"]),
    )


def build_cycle_config(
    profile: ProfileSettings, scanner: ScannerSettings, settings: dict
) -> CycleConfig:
    swing = int(settings.get("detect", {}).get("swing_length", 5))
    signal_tf = profile.signal_timeframes[0]
    # кулдаун в барах: секунды кулдауна / секунды бара сигнального ТФ
    tf_sec = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(signal_tf, 900)
    return CycleConfig(
        signal_timeframe=signal_tf,
        context_timeframes=tuple(
            tf for tf in (profile.signal_timeframes[1:] + profile.context_timeframes)
            if tf != signal_tf
        ),
        min_score=scanner.min_opportunity_score,
        cooldown_bars=max(1, scanner.per_symbol_cooldown_seconds // tf_sec),
        approval_ttl_seconds=profile.approval_ttl_seconds,
        smc=SmcConfig(swing_length=swing),
    )
