"""Momentum analysis engine (deterministic).

Scores a pair 0-100 from short-horizon acceleration signals. All weights and
thresholds live in MomentumConfig, versioned via the scoring engine, never
buried in code paths.

Components:
  price_acceleration   5m vs 1h price change (recent move stronger than trend)
  volume_intensity     recent volume relative to liquidity
  volume_acceleration  5m volume pace vs 1h pace
  buy_pressure         buy/sell transaction imbalance
  trend                1h and 6h direction agreement
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tradeos.providers.base import PairData


@dataclass
class MomentumConfig:
    version: str = "momentum-v1"
    w_price_acceleration: float = 0.25
    w_volume_intensity: float = 0.20
    w_volume_acceleration: float = 0.20
    w_buy_pressure: float = 0.20
    w_trend: float = 0.15
    # volume_1h at this multiple of liquidity scores 100 intensity
    intensity_full_scale: float = 0.5


@dataclass
class MomentumResult:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    version: str = "momentum-v1"


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def analyze_momentum(pair: PairData, cfg: MomentumConfig | None = None) -> MomentumResult:
    cfg = cfg or MomentumConfig()

    # price acceleration: 5m change annualized against 1h change
    rate_5m = pair.price_change_5m * 12          # per-hour pace implied by last 5m
    accel = rate_5m - pair.price_change_1h
    price_acceleration = _clamp(50 + accel * 2)

    # volume intensity: recent hourly volume vs pool liquidity
    if pair.liquidity_usd > 0:
        intensity_ratio = pair.volume_1h / pair.liquidity_usd
        volume_intensity = _clamp(intensity_ratio / cfg.intensity_full_scale * 100)
    else:
        volume_intensity = 0.0

    # volume acceleration: 5m pace vs average 5m share of the hour
    if pair.volume_1h > 0:
        pace = pair.volume_5m / (pair.volume_1h / 12)
        volume_acceleration = _clamp((pace - 1) * 50 + 50)
    else:
        volume_acceleration = 0.0

    # buy pressure over 5m and 1h windows
    def pressure(buys: int, sells: int) -> float:
        total = buys + sells
        if total == 0:
            return 50.0
        return buys / total * 100

    buy_pressure = 0.6 * pressure(pair.buys_5m, pair.sells_5m) + \
        0.4 * pressure(pair.buys_1h, pair.sells_1h)

    # trend agreement
    trend = 50.0
    if pair.price_change_1h > 0 and pair.price_change_6h > 0:
        trend = _clamp(50 + min(pair.price_change_1h, 25))
    elif pair.price_change_1h < 0 and pair.price_change_6h < 0:
        trend = _clamp(50 + max(pair.price_change_1h, -25))
    elif pair.price_change_1h > 0:
        trend = 55.0  # early reversal, mildly positive

    components = {
        "price_acceleration": round(price_acceleration, 2),
        "volume_intensity": round(volume_intensity, 2),
        "volume_acceleration": round(volume_acceleration, 2),
        "buy_pressure": round(buy_pressure, 2),
        "trend": round(trend, 2),
    }
    score = (
        cfg.w_price_acceleration * price_acceleration
        + cfg.w_volume_intensity * volume_intensity
        + cfg.w_volume_acceleration * volume_acceleration
        + cfg.w_buy_pressure * buy_pressure
        + cfg.w_trend * trend
    )
    return MomentumResult(score=round(_clamp(score), 2), components=components,
                          version=cfg.version)
