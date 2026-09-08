"""Opportunity scoring engine — configurable and versioned.

Combines momentum, liquidity, volume, token-safety heuristics, and (when
available) smart-money/whale/holder data into an overall 0-100 score plus a
0-100 risk score. Weights are explicit in ScoringConfig; every stored
opportunity records the scoring version used.

Token-safety heuristics here are deterministic red flags from market data
(too young, thin liquidity, one-sided flow, suspicious volume/liquidity
ratio). Full contract analysis belongs to the on-chain agent when an RPC /
indexer is configured.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from tradeos.providers.base import PairData
from tradeos.strategies.momentum import MomentumConfig, analyze_momentum


@dataclass
class ScoringConfig:
    version: str = "scoring-v1"
    w_momentum: float = 0.35
    w_liquidity: float = 0.20
    w_volume: float = 0.15
    w_safety: float = 0.20
    w_smart_money: float = 0.10
    liquidity_full_scale_usd: float = 250_000.0
    volume_full_scale_usd: float = 500_000.0
    momentum: MomentumConfig = field(default_factory=MomentumConfig)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class OpportunityScore:
    overall: float
    risk: float                       # 0 = safest, 100 = maximal risk
    momentum: float
    smart_money: float | None
    whale: float | None
    components: dict
    red_flags: list[str]
    version: str


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def safety_red_flags(pair: PairData, min_age_hours: float,
                     min_liquidity_usd: float) -> list[str]:
    flags: list[str] = []
    age = pair.age_hours
    if age is not None and age < min_age_hours:
        flags.append(f"token age {age:.2f}h below minimum {min_age_hours}h")
    if pair.liquidity_usd < min_liquidity_usd:
        flags.append(f"liquidity ${pair.liquidity_usd:,.0f} below minimum "
                     f"${min_liquidity_usd:,.0f}")
    if pair.liquidity_usd > 0 and pair.volume_24h / pair.liquidity_usd > 50:
        flags.append("volume/liquidity ratio > 50: possible wash trading")
    total_1h = pair.buys_1h + pair.sells_1h
    if total_1h >= 20 and pair.sells_1h == 0:
        flags.append("zero sells in 1h with active buying: possible honeypot")
    if pair.price_change_24h > 500:
        flags.append(f"price +{pair.price_change_24h:.0f}%/24h: extreme extension")
    if pair.price_usd <= 0:
        flags.append("no price available")
    return flags


def score_opportunity(pair: PairData, cfg: ScoringConfig,
                      min_age_hours: float, min_liquidity_usd: float,
                      smart_money_score: float | None = None,
                      whale_score: float | None = None) -> OpportunityScore:
    momentum = analyze_momentum(pair, cfg.momentum)
    flags = safety_red_flags(pair, min_age_hours, min_liquidity_usd)

    liquidity_score = _clamp(pair.liquidity_usd / cfg.liquidity_full_scale_usd * 100)
    volume_score = _clamp(pair.volume_24h / cfg.volume_full_scale_usd * 100)
    safety_score = _clamp(100 - len(flags) * 30)
    smart = smart_money_score if smart_money_score is not None else 50.0

    overall = (
        cfg.w_momentum * momentum.score
        + cfg.w_liquidity * liquidity_score
        + cfg.w_volume * volume_score
        + cfg.w_safety * safety_score
        + cfg.w_smart_money * smart
    )

    # Risk: red flags dominate; thin liquidity and extreme moves add to it.
    risk = _clamp(
        len(flags) * 25
        + (25 if pair.liquidity_usd < min_liquidity_usd * 2 else 0)
        + min(abs(pair.price_change_24h) / 10, 25)
    )

    return OpportunityScore(
        overall=round(_clamp(overall), 2),
        risk=round(risk, 2),
        momentum=momentum.score,
        smart_money=smart_money_score,
        whale=whale_score,
        components={
            "momentum": momentum.components,
            "liquidity_score": round(liquidity_score, 2),
            "volume_score": round(volume_score, 2),
            "safety_score": round(safety_score, 2),
        },
        red_flags=flags,
        version=cfg.version,
    )
