"""Application assembly: builds every component with explicit wiring."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tradeos.agents.coding_agent import CodingAgent
from tradeos.agents.critic import CriticAgent
from tradeos.agents.executive import ExecutiveAgent
from tradeos.agents.financial import FinancialAgent
from tradeos.agents.onchain import OnChainAgent
from tradeos.agents.planning import PlanningAgent
from tradeos.agents.research import ResearchAgent
from tradeos.agents.risk_agent import RiskAgent
from tradeos.agents.web_agent import WebAgent
from tradeos.config import Mode, Settings, get_settings
from tradeos.db.database import Database
from tradeos.execution.gateway import ExecutionGateway
from tradeos.learning.post_trade import ensure_live_strategy
from tradeos.llm.client import LlmClient
from tradeos.logging_setup import setup_logging
from tradeos.memory.stores import MemoryStore
from tradeos.orchestration.monitor import PositionMonitor
from tradeos.orchestration.pipeline import OpportunityPipeline
from tradeos.orchestration.scheduler import Scheduler
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.chains import build_chain_providers
from tradeos.providers.chains.helius import HeliusProvider
from tradeos.providers.dexscreener import DexScreenerProvider
from tradeos.risk.engine import RiskEngine
from tradeos.risk.killswitch import KillSwitch
from tradeos.strategies.scoring import ScoringConfig
from tradeos.wallets.reputation import WalletReputationStore
from tradeos.wallets.scanner import SmartMoneyScanner
from tradeos.wallets.webhooks import HeliusWebhookManager

logger = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    db: Database
    kill_switch: KillSwitch
    risk_engine: RiskEngine
    accounting: PortfolioAccounting
    gateway: ExecutionGateway
    market: DexScreenerProvider
    pipeline: OpportunityPipeline
    monitor: PositionMonitor
    scheduler: Scheduler
    llm: LlmClient
    memory: MemoryStore
    reputation: WalletReputationStore
    agents: dict
    smartmoney_scanner: SmartMoneyScanner | None = None
    webhook_manager: HeliusWebhookManager | None = None


def build_app(settings: Settings | None = None) -> App:
    settings = settings or get_settings()
    setup_logging()

    db = Database(settings.database_path)
    kill_switch = KillSwitch(db)
    risk_engine = RiskEngine(settings, db, kill_switch)
    if risk_engine.policy is None:
        logger.warning("risk policy incomplete — trading disabled: %s",
                       risk_engine.policy_errors)
        db.system_event("risk_policy_incomplete", "; ".join(risk_engine.policy_errors))

    mode_str = settings.mode.value if settings.mode != Mode.DEVELOPMENT else "paper"
    accounting = PortfolioAccounting(db, mode=mode_str)
    if settings.mode != Mode.LIVE:
        accounting.ensure_seeded(settings.paper_starting_balance_usd)

    gateway = ExecutionGateway(settings, db, risk_engine, kill_switch, accounting)
    llm = LlmClient(settings)
    market = DexScreenerProvider()
    chain_providers = build_chain_providers()
    memory = MemoryStore(db)
    reputation = WalletReputationStore(db)

    scanner = None
    solana_provider = chain_providers.get("solana")
    if isinstance(solana_provider, HeliusProvider):
        scanner = SmartMoneyScanner(settings, db, solana_provider, reputation)
        logger.info("Helius indexer configured: smart-money scanner enabled")
    else:
        logger.info("no TRADEOS_HELIUS_API_KEY: smart-money scanner disabled")

    agents = {
        "research": ResearchAgent(settings, db, llm),
        "onchain": OnChainAgent(settings, db, llm, chain_providers),
        "financial": FinancialAgent(settings, db, llm),
        "risk": RiskAgent(settings, db, llm),
        "critic": CriticAgent(settings, db, llm),
        "executive": ExecutiveAgent(settings, db, llm),
        "planning": PlanningAgent(settings, db, llm),
        "web": WebAgent(settings, db, llm),
        "coding": CodingAgent(settings, db, llm),
    }

    scoring_config = ScoringConfig()
    ensure_live_strategy(db, "momentum", scoring_config.version, scoring_config.to_json())

    pipeline = OpportunityPipeline(
        settings, db, agents["research"], agents["onchain"], agents["financial"],
        agents["risk"], agents["critic"], agents["executive"], gateway, reputation,
        scoring_config,
    )
    monitor = PositionMonitor(settings, db, market, gateway, llm)

    webhook_manager = None
    if scanner is not None:
        webhook_manager = HeliusWebhookManager(
            settings, db, solana_provider, reputation, scanner,
            pipeline=pipeline, market=market)

    scheduler = Scheduler(settings, db, market, pipeline, monitor,
                          smartmoney_scanner=scanner,
                          webhook_manager=webhook_manager)

    return App(settings, db, kill_switch, risk_engine, accounting, gateway,
               market, pipeline, monitor, scheduler, llm, memory, reputation,
               agents, scanner, webhook_manager)
