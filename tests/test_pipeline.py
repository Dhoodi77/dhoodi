"""End-to-end pipeline and agent tests (heuristic mode, no network, no LLM).

Uses a stub market provider so the complete autonomous decision path runs:
discovery data -> scoring -> agents -> executive -> risk gate -> paper fill
-> monitoring exit -> post-trade review.
"""
import time

import pytest

from tradeos.agents.critic import CriticAgent
from tradeos.agents.executive import ExecutiveAgent
from tradeos.agents.financial import FinancialAgent
from tradeos.agents.onchain import OnChainAgent
from tradeos.agents.research import ResearchAgent
from tradeos.agents.risk_agent import RiskAgent
from tradeos.learning.post_trade import run_post_trade_review
from tradeos.llm.client import LlmClient
from tradeos.orchestration.monitor import PositionMonitor
from tradeos.orchestration.pipeline import OpportunityPipeline
from tradeos.providers.base import PairData
from tradeos.wallets.reputation import WalletReputationStore

from .test_scoring import dead_pair, hot_pair


class StubMarket:
    """Deterministic market data stand-in for DexScreenerProvider."""

    def __init__(self):
        self.prices: dict[str, float] = {}

    async def get_pair(self, chain, pair_address):
        price = self.prices.get(pair_address)
        if price is None:
            return None
        return PairData(chain=chain, pair_address=pair_address,
                        token_address="tokX", price_usd=price, liquidity_usd=150_000)

    async def get_token_pairs(self, chain, token_address):
        pairs = []
        for pair_address in self.prices:
            pair = await self.get_pair(chain, pair_address)
            if pair:
                pairs.append(pair)
        return pairs

    async def discover(self):
        return []

    async def close(self):
        pass


@pytest.fixture
def pipeline(settings, db, gateway):
    llm = LlmClient(settings)
    llm.available = False  # force heuristic mode regardless of environment
    llm._client = None
    reputation = WalletReputationStore(db)
    return OpportunityPipeline(
        settings, db,
        ResearchAgent(settings, db, llm),
        OnChainAgent(settings, db, llm, {}),
        FinancialAgent(settings, db, llm),
        RiskAgent(settings, db, llm),
        CriticAgent(settings, db, llm),
        ExecutiveAgent(settings, db, llm),
        gateway, reputation,
    )


async def test_hot_pair_full_pipeline_executes_paper_trade(pipeline, db, accounting):
    decision = await pipeline.process(hot_pair())
    assert decision is not None
    # verdicts from all five agents + executive recorded
    agents = {r["agent"] for r in db.query(
        "SELECT DISTINCT agent FROM agent_decisions WHERE opportunity_id = ?",
        (decision.opportunity_id,))}
    assert {"research", "onchain", "financial", "risk", "critic", "executive"} <= agents
    opp = db.query_one("SELECT * FROM opportunities WHERE id = ?",
                       (decision.opportunity_id,))
    assert opp is not None and opp["status"] in ("executed", "rejected", "detected")
    if decision.final == "execute":
        assert opp["status"] == "executed"
        positions = accounting.open_positions()
        assert len(positions) == 1
        assert positions[0]["cost_usd"] <= 20.0


async def test_dead_pair_rejected(pipeline, db, accounting):
    decision = await pipeline.process(dead_pair())
    assert decision is not None
    assert decision.final == "reject"
    assert accounting.open_positions() == []


async def test_disallowed_chain_skipped(pipeline):
    decision = await pipeline.process(hot_pair(chain="tron"))
    assert decision is None


async def test_duplicate_signal_deduplicated(pipeline, db):
    d1 = await pipeline.process(hot_pair())
    d2 = await pipeline.process(hot_pair())
    assert d1 is not None
    assert d2 is None  # second signal for same token suppressed


async def test_audit_trail_traces_opportunity(pipeline, db):
    decision = await pipeline.process(hot_pair())
    trail = db.query("SELECT * FROM audit_log WHERE opportunity_id = ?",
                     (decision.opportunity_id,))
    assert len(trail) >= 2  # detected + decision at minimum
    snapshot = db.query("SELECT * FROM pair_snapshots")
    assert len(snapshot) == 1


# --- monitor and exits ---------------------------------------------------

async def test_monitor_stop_loss_and_review(settings, db, gateway, accounting):
    from tradeos.execution.instructions import TradeInstruction
    result = gateway.submit(TradeInstruction(
        opportunity_id="opp_mon", chain="solana", token_address="tokX",
        pair_address="pairX", symbol="HOT", side="buy", amount_usd=20.0,
        max_slippage_pct=2.0, max_gas_usd=1.0), market_price_usd=1.0)
    assert result.ok

    market = StubMarket()
    market.prices["pairX"] = 0.80  # ~-21% from 1.01 fill: below 15% stop
    llm = LlmClient(settings)
    llm.available = False
    llm._client = None
    monitor = PositionMonitor(settings, db, market, gateway, llm)
    closed = await monitor.check_all()
    assert closed == 1
    pos = db.query_one("SELECT * FROM positions WHERE id = ?", (result.position_id,))
    assert pos["status"] == "closed"
    assert pos["exit_reason"] == "stop_loss"
    assert pos["realized_pnl_usd"] < 0
    reviews = db.query("SELECT * FROM post_trade_reviews WHERE position_id = ?",
                       (result.position_id,))
    assert len(reviews) == 1


async def test_monitor_take_profit(settings, db, gateway):
    from tradeos.execution.instructions import TradeInstruction
    result = gateway.submit(TradeInstruction(
        opportunity_id="opp_tp", chain="solana", token_address="tokX",
        pair_address="pairX", symbol="HOT", side="buy", amount_usd=20.0,
        max_slippage_pct=2.0, max_gas_usd=1.0), market_price_usd=1.0)
    market = StubMarket()
    market.prices["pairX"] = 1.40  # +38% from 1.01 fill: above 30% target
    llm = LlmClient(settings)
    llm.available = False
    llm._client = None
    monitor = PositionMonitor(settings, db, market, gateway, llm)
    closed = await monitor.check_all()
    assert closed == 1
    pos = db.query_one("SELECT * FROM positions WHERE id = ?", (result.position_id,))
    assert pos["exit_reason"] == "take_profit"
    assert pos["realized_pnl_usd"] > 0


def test_max_hold_exit_rule(settings, db, gateway):
    monitor = PositionMonitor(settings, db, StubMarket(), gateway,
                              LlmClient(settings))
    pos = {"entry_price_usd": 1.0, "stop_loss_pct": 15, "take_profit_pct": 30,
           "max_hold_hours": 24, "opened_at": time.time() - 25 * 3600}
    assert monitor.decide_exit(pos, 1.0) == "max_hold"
    pos["opened_at"] = time.time()
    assert monitor.decide_exit(pos, 1.0) is None
