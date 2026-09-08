"""Wallet reputation / smart-money scoring.

Scores 0-100 from observed performance (win rate, average return,
consistency, sample size), with time decay: a wallet is never permanently
'smart money' — without fresh scoring events its effective score decays
toward neutral (50) with a configurable half-life.

Real wallet trade histories come from chain providers/indexers; this module
owns the math and persistence, and works identically on paper-observed data.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

DECAY_HALF_LIFE_DAYS = 14.0
NEUTRAL_SCORE = 50.0


@dataclass
class WalletPerformance:
    trade_count: int
    win_rate: float          # 0..1
    avg_return_pct: float
    return_stddev_pct: float
    avg_entry_delay_s: float | None = None  # how early after launch it enters


def compute_smart_money_score(perf: WalletPerformance) -> float:
    """0..100. Requires a real sample; tiny histories stay near neutral."""
    if perf.trade_count <= 0:
        return NEUTRAL_SCORE
    # sample-size confidence: 50% weight at ~10 trades, ~90% at 40+
    confidence = 1 - math.exp(-perf.trade_count / 14)

    win_component = perf.win_rate * 100                       # 0..100
    return_component = max(0.0, min(100.0, 50 + perf.avg_return_pct))
    consistency = 100 / (1 + perf.return_stddev_pct / 50) if perf.return_stddev_pct >= 0 else 50

    raw = 0.45 * win_component + 0.35 * return_component + 0.20 * consistency
    return round(NEUTRAL_SCORE + (raw - NEUTRAL_SCORE) * confidence, 2)


def decayed_score(score: float, scored_at: float, now: float | None = None) -> float:
    """Decay toward neutral with a 14-day half-life."""
    now = now or time.time()
    age_days = max(0.0, (now - scored_at) / 86400)
    factor = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
    return round(NEUTRAL_SCORE + (score - NEUTRAL_SCORE) * factor, 2)


def classify(score: float, trade_count: int) -> str:
    if trade_count < 5:
        return "neutral"
    if score >= 70:
        return "smart_money"
    if score <= 30:
        return "suspicious"
    return "neutral"


class WalletReputationStore:
    def __init__(self, db):
        self.db = db

    def record_score(self, chain: str, address: str, perf: WalletPerformance) -> float:
        score = compute_smart_money_score(perf)
        self.db.execute(
            "INSERT INTO wallet_scores (chain, address, score, win_rate, avg_return_pct, "
            "trade_count, classification, scored_at, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chain, address, score, perf.win_rate, perf.avg_return_pct,
             perf.trade_count, classify(score, perf.trade_count), time.time(),
             json.dumps(perf.__dict__)),
        )
        return score

    def current_score(self, chain: str, address: str) -> float:
        row = self.db.query_one(
            "SELECT score, scored_at FROM wallet_scores WHERE chain = ? AND address = ? "
            "ORDER BY scored_at DESC LIMIT 1", (chain, address))
        if row is None:
            return NEUTRAL_SCORE
        return decayed_score(row["score"], row["scored_at"])

    def tracked_smart_money(self, min_score: float = 65.0) -> list[dict]:
        rows = self.db.query(
            "SELECT chain, address, MAX(scored_at) AS scored_at, score, classification "
            "FROM wallet_scores GROUP BY chain, address")
        out = []
        for r in rows:
            effective = decayed_score(r["score"], r["scored_at"])
            if effective >= min_score:
                out.append({**r, "effective_score": effective})
        return sorted(out, key=lambda r: -r["effective_score"])
