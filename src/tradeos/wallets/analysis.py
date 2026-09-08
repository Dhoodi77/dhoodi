"""Wallet trade-history analysis: swaps -> WalletPerformance.

Works on SOL-denominated round trips only (SOL -> token -> SOL), matched
per mint with average cost basis. Sells with no observed prior buy are
ignored — history windows are finite and attributing unknown cost would
fabricate performance. This makes scores conservative rather than flattering.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tradeos.wallets.reputation import WalletPerformance


@dataclass
class RoundTrip:
    token_mint: str
    cost_sol: float
    proceeds_sol: float
    closed_at: float

    @property
    def return_pct(self) -> float:
        if self.cost_sol <= 0:
            return 0.0
        return (self.proceeds_sol - self.cost_sol) / self.cost_sol * 100


def extract_round_trips(swaps: list[dict]) -> list[RoundTrip]:
    """swaps: dict rows with token_mint, direction, token_amount, sol_amount,
    block_time. Processed chronologically per mint with average cost basis."""
    positions: dict[str, dict] = {}  # mint -> {qty, cost_sol}
    trips: list[RoundTrip] = []
    for swap in sorted(swaps, key=lambda s: s.get("block_time") or 0):
        mint = swap.get("token_mint")
        qty = float(swap.get("token_amount") or 0)
        sol = float(swap.get("sol_amount") or 0)
        if not mint or qty <= 0 or sol <= 0:
            continue
        pos = positions.setdefault(mint, {"qty": 0.0, "cost_sol": 0.0})
        if swap.get("direction") == "buy":
            pos["qty"] += qty
            pos["cost_sol"] += sol
        elif swap.get("direction") == "sell":
            if pos["qty"] <= 0:
                continue  # unattributable: bought outside the window
            matched_qty = min(qty, pos["qty"])
            cost_removed = pos["cost_sol"] * (matched_qty / pos["qty"])
            proceeds = sol * (matched_qty / qty)
            trips.append(RoundTrip(mint, cost_removed, proceeds,
                                   float(swap.get("block_time") or 0)))
            pos["qty"] -= matched_qty
            pos["cost_sol"] -= cost_removed
            if pos["qty"] <= 1e-12:
                pos["qty"], pos["cost_sol"] = 0.0, 0.0
    return trips


def analyze_wallet_swaps(swaps: list[dict]) -> WalletPerformance | None:
    """None when there is not a single completed round trip to judge."""
    trips = extract_round_trips(swaps)
    if not trips:
        return None
    returns = [t.return_pct for t in trips]
    n = len(returns)
    mean = sum(returns) / n
    stddev = math.sqrt(sum((r - mean) ** 2 for r in returns) / n)
    wins = sum(1 for r in returns if r > 0)
    return WalletPerformance(
        trade_count=n,
        win_rate=wins / n,
        avg_return_pct=round(mean, 2),
        return_stddev_pct=round(stddev, 2),
    )
