"""Post-trade critic and learning pipeline.

After every closed position a structured review is generated and stored
permanently. Reviews feed the performance record that future strategy
evaluation runs against. Strategy configs themselves are versioned in
strategy_versions and are NEVER modified automatically: a proposal must be
evaluated, tested, compared, recorded, and explicitly approved before going
live (status transitions proposed -> testing -> live are manual policy
actions).
"""
from __future__ import annotations

import json
import time

from tradeos.db.database import Database
from tradeos.llm.client import LlmClient


def build_review(db: Database, position: dict) -> dict:
    """Deterministic core of the post-trade review."""
    opp = db.query_one("SELECT * FROM opportunities WHERE id = ?",
                       (position["opportunity_id"],)) if position["opportunity_id"] else None
    decisions = db.query(
        "SELECT agent, verdict, confidence, reasoning FROM agent_decisions "
        "WHERE opportunity_id = ?", (position["opportunity_id"],))
    trades = db.query("SELECT * FROM trades WHERE position_id = ?", (position["id"],))

    pnl = position.get("realized_pnl_usd") or 0.0
    entry, exit_price = position["entry_price_usd"], position.get("exit_price_usd")
    pnl_pct = ((exit_price - entry) / entry * 100) if (exit_price and entry) else None
    hold_hours = ((position.get("closed_at") or time.time()) - position["opened_at"]) / 3600

    hit_stop = position.get("exit_reason") == "stop_loss"
    hit_target = position.get("exit_reason") == "take_profit"

    return {
        "position_id": position["id"],
        "symbol": position.get("symbol"),
        "entry_thesis": (opp or {}).get("data_snapshot_json") and "recorded in opportunity snapshot",
        "supporting_verdicts": [d for d in decisions if d["verdict"] in ("buy", "approve")],
        "contradicting_verdicts": [d for d in decisions if d["verdict"] in ("reject", "needs_evidence")],
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": position.get("exit_reason"),
        "pnl_usd": pnl,
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "hold_hours": round(hold_hours, 2),
        "slippage_pct": [t["slippage_pct"] for t in trades],
        "followed_strategy": hit_stop or hit_target or position.get("exit_reason") == "max_hold",
        "followed_risk_rules": True,  # positions only exist via the risk-gated gateway
        "what_went_right": "target reached" if pnl > 0 else None,
        "what_went_wrong": (position.get("exit_reason") if pnl < 0 else None),
        "overall_score_at_entry": (opp or {}).get("overall_score"),
        # Entry signals recorded verbatim so weights can be validated against
        # realized outcomes instead of staying untested heuristics.
        "signals_at_entry": {
            "momentum": (opp or {}).get("momentum_score"),
            "smart_money": (opp or {}).get("smart_money_score"),
            "whale": (opp or {}).get("whale_score"),
            "risk": (opp or {}).get("risk_score"),
        },
    }


async def run_post_trade_review(db: Database, llm: LlmClient, position: dict) -> int:
    review = build_review(db, position)

    if llm.available:
        lessons = await llm.complete_json(
            "reasoning",
            "You are the post-trade critic of a trading system. Given the structured "
            'review, extract lessons. Respond with JSON: {"lessons": [str], '
            '"agent_errors": [str], "summary": str}.',
            json.dumps(review, default=str),
        )
        if lessons is not None:
            review["lessons"] = lessons.get("lessons", [])[:10]
            review["agent_errors"] = lessons.get("agent_errors", [])[:10]
            review["llm_summary"] = str(lessons.get("summary", ""))[:1000]

    review_id = db.execute(
        "INSERT INTO post_trade_reviews (position_id, opportunity_id, review_json, "
        "pnl_usd, followed_strategy, followed_risk_rules, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (position["id"], position.get("opportunity_id"),
         json.dumps(review, default=str), review["pnl_usd"],
         1 if review["followed_strategy"] else 0, 1, time.time()),
    )
    db.audit("post_trade_critic", "review_created", position.get("opportunity_id"),
             {"position_id": position["id"], "pnl_usd": review["pnl_usd"]})
    return review_id


def signal_performance(db: Database) -> dict:
    """Realized outcomes bucketed by the entry signals that argued for the
    trade. This is the evidence base for tuning scoring weights; buckets with
    tiny samples are reported as-is and marked low-confidence rather than
    hidden or smoothed."""
    rows = db.query(
        "SELECT o.momentum_score, o.smart_money_score, o.whale_score, r.pnl_usd "
        "FROM post_trade_reviews r JOIN opportunities o ON o.id = r.opportunity_id "
        "WHERE r.pnl_usd IS NOT NULL")

    def bucket(name: str, selector) -> dict:
        sample = [r for r in rows if selector(r)]
        wins = sum(1 for r in sample if (r["pnl_usd"] or 0) > 0)
        return {
            "bucket": name,
            "trades": len(sample),
            "wins": wins,
            "win_rate": round(wins / len(sample), 3) if sample else None,
            "total_pnl_usd": round(sum(r["pnl_usd"] or 0 for r in sample), 2),
            "low_confidence": len(sample) < 20,
        }

    return {
        "closed_trades": len(rows),
        "buckets": [
            bucket("momentum>=65", lambda r: (r["momentum_score"] or 0) >= 65),
            bucket("momentum<65", lambda r: (r["momentum_score"] or 0) < 65),
            bucket("smart_money_present",
                   lambda r: r["smart_money_score"] is not None),
            bucket("smart_money>=65",
                   lambda r: (r["smart_money_score"] or 0) >= 65),
            bucket("whale_present", lambda r: r["whale_score"] is not None),
            bucket("whale>=60", lambda r: (r["whale_score"] or 0) >= 60),
            bucket("no_onchain_signals",
                   lambda r: r["smart_money_score"] is None
                   and r["whale_score"] is None),
        ],
        "note": "buckets under 20 trades are low-confidence; do not retune "
                "weights on them",
    }


def ensure_live_strategy(db: Database, name: str, version: str, config_json: str) -> None:
    """Register the initial strategy version as live if none exists."""
    row = db.query_one(
        "SELECT id FROM strategy_versions WHERE name = ? AND status = 'live'", (name,))
    if row is None:
        db.execute(
            "INSERT OR IGNORE INTO strategy_versions (name, version, config_json, status, "
            "created_at) VALUES (?, ?, ?, 'live', ?)",
            (name, version, config_json, time.time()),
        )
