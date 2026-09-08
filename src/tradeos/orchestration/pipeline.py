"""The decision pipeline.

DATA -> DETECTION -> RESEARCH -> ON-CHAIN -> FINANCIAL -> RISK -> CRITIC
-> EXECUTIVE -> FINAL RISK CHECK (RiskEngine, deterministic) -> EXECUTION.

No single AI response can trigger a trade: agent output only ever becomes a
DecisionObject; this module converts an 'execute' decision into a
TradeInstruction with deterministic sizing, and the ExecutionGateway
re-validates everything against the risk policy before any fill.
"""
from __future__ import annotations

import json
import logging
import time

from tradeos.agents.critic import CriticAgent
from tradeos.agents.executive import DecisionObject, ExecutiveAgent
from tradeos.agents.financial import FinancialAgent
from tradeos.agents.onchain import OnChainAgent
from tradeos.agents.research import ResearchAgent
from tradeos.agents.risk_agent import RiskAgent
from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.execution.gateway import ExecutionGateway
from tradeos.execution.instructions import TradeInstruction
from tradeos.logging_setup import new_correlation_id, set_correlation_id
from tradeos.providers.base import PairData
from tradeos.strategies.scoring import ScoringConfig, score_opportunity
from tradeos.wallets.reputation import WalletReputationStore

logger = logging.getLogger(__name__)


class OpportunityPipeline:
    def __init__(self, settings: Settings, db: Database,
                 research: ResearchAgent, onchain: OnChainAgent,
                 financial: FinancialAgent, risk_agent: RiskAgent,
                 critic: CriticAgent, executive: ExecutiveAgent,
                 gateway: ExecutionGateway, reputation: WalletReputationStore,
                 scoring_config: ScoringConfig | None = None):
        self.settings = settings
        self.db = db
        self.research = research
        self.onchain = onchain
        self.financial = financial
        self.risk_agent = risk_agent
        self.critic = critic
        self.executive = executive
        self.gateway = gateway
        self.reputation = reputation
        self.scoring = scoring_config or ScoringConfig()

    def _store_snapshot(self, pair: PairData) -> None:
        self.db.execute(
            "INSERT INTO pair_snapshots (chain, pair_address, token_address, dex, price_usd, "
            "liquidity_usd, volume_24h, volume_1h, volume_5m, price_change_5m, price_change_1h, "
            "price_change_6h, price_change_24h, buys_5m, sells_5m, buys_1h, sells_1h, "
            "captured_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pair.chain, pair.pair_address, pair.token_address, pair.dex, pair.price_usd,
             pair.liquidity_usd, pair.volume_24h, pair.volume_1h, pair.volume_5m,
             pair.price_change_5m, pair.price_change_1h, pair.price_change_6h,
             pair.price_change_24h, pair.buys_5m, pair.sells_5m, pair.buys_1h,
             pair.sells_1h, pair.captured_at, json.dumps(pair.raw, default=str)),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO tokens (chain, address, symbol, name, first_seen_at, "
            "pair_created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (pair.chain, pair.token_address, pair.symbol, pair.name,
             time.time(), pair.pair_created_at),
        )

    def _already_active(self, pair: PairData) -> bool:
        row = self.db.query_one(
            "SELECT id FROM opportunities WHERE chain = ? AND token_address = ? "
            "AND status IN ('detected', 'analyzing', 'approved', 'executed') "
            "AND created_at > ?",
            (pair.chain, pair.token_address, time.time() - 6 * 3600))
        if row:
            return True
        pos = self.db.query_one(
            "SELECT id FROM positions WHERE chain = ? AND token_address = ? "
            "AND status = 'open'", (pair.chain, pair.token_address))
        return pos is not None

    async def process(self, pair: PairData) -> DecisionObject | None:
        """Run one pair through the full pipeline. Returns the decision, or
        None if it never reached the agents (dedup / obviously unfit)."""
        if pair.chain not in self.settings.allowed_chain_list:
            return None
        if self._already_active(pair):
            return None

        opp_id = new_correlation_id("opp")
        set_correlation_id(opp_id)
        self._store_snapshot(pair)

        smart, whale = self.reputation.token_signals(
            pair.chain, pair.token_address,
            smart_threshold=self.settings.smartmoney_score_threshold,
            whale_sol_threshold=self.settings.whale_sol_threshold,
            whale_usd_threshold=self.settings.whale_usd_threshold,
        )
        score = score_opportunity(
            pair, self.scoring,
            self.settings.min_token_age_hours, self.settings.min_liquidity_usd,
            smart_money_score=smart, whale_score=whale,
        )

        now = time.time()
        self.db.execute(
            "INSERT INTO opportunities (id, chain, token_address, pair_address, symbol, "
            "source, status, momentum_score, smart_money_score, whale_score, risk_score, "
            "overall_score, scoring_version, data_snapshot_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (opp_id, pair.chain, pair.token_address, pair.pair_address, pair.symbol,
             "dexscreener_discovery", "analyzing", score.momentum, score.smart_money,
             score.whale, score.risk, score.overall, score.version,
             json.dumps(pair.raw, default=str), now, now),
        )
        self.db.audit("pipeline", "opportunity_detected", opp_id,
                      {"symbol": pair.symbol, "overall": score.overall,
                       "risk": score.risk, "red_flags": score.red_flags})

        # Hard pre-filter: obvious unfit tokens don't consume agent/LLM budget.
        if score.red_flags and score.risk >= 75:
            self._set_status(opp_id, "rejected")
            self.db.audit("pipeline", "prefiltered", opp_id, {"red_flags": score.red_flags})
            return DecisionObject(opp_id, "reject",
                                  "prefilter: " + "; ".join(score.red_flags))

        # Agent analysis
        verdicts = [
            await self.research.analyze(opp_id, pair),
            await self.onchain.analyze(opp_id, pair),
            await self.financial.analyze(opp_id, pair),
            await self.risk_agent.analyze(opp_id, pair, score),
        ]
        thesis = (f"momentum entry on {pair.symbol} ({pair.chain}): overall score "
                  f"{score.overall}, momentum {score.momentum}")
        verdicts.append(await self.critic.analyze(opp_id, pair, score, thesis))

        decision = await self.executive.decide(opp_id, verdicts)

        if decision.final == "execute":
            self._set_status(opp_id, "approved")
            executed = await self._execute(opp_id, pair)
            self._set_status(opp_id, "executed" if executed else "rejected")
        elif decision.final == "investigate":
            self._set_status(opp_id, "detected")  # stays visible for re-analysis
        else:
            self._set_status(opp_id, "rejected")
        set_correlation_id(None)
        return decision

    async def _execute(self, opp_id: str, pair: PairData) -> bool:
        # Deterministic sizing: configured cap bounded by the hard $20 limit
        # (enforced again inside the risk engine).
        policy = self.gateway.risk_engine.policy
        if policy is None:
            return False
        instr = TradeInstruction(
            opportunity_id=opp_id,
            chain=pair.chain,
            token_address=pair.token_address,
            pair_address=pair.pair_address,
            symbol=pair.symbol,
            side="buy",
            amount_usd=policy.max_initial_position_usd,
            max_slippage_pct=min(self.settings.paper_simulated_slippage_pct * 2,
                                 policy.max_slippage_pct),
            max_gas_usd=policy.max_gas_usd,
            reason="pipeline momentum entry",
        )
        result = await self.gateway.submit_async(instr, pair.price_usd)
        if not result.ok:
            logger.warning("execution declined for %s: %s", opp_id, result.error)
            return False
        self.db.alert("high", f"Opened position: {pair.symbol}",
                      f"${instr.amount_usd:.2f} at ~${result.fill_price:.8f} ({opp_id})")
        return True

    def _set_status(self, opp_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE opportunities SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), opp_id))
