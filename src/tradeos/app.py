"""Application assembly: builds every component with explicit wiring."""
from __future__ import annotations

import logging
import os
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
from tradeos.execution.evm_live import EvmLiveExecutor
from tradeos.execution.live import LiveExecutionEngine
from tradeos.execution.signer_client import RemoteSignerClient
from tradeos.execution.venues.jupiter import JupiterVenue
from tradeos.execution.venues.zerox import ZeroExVenue
from tradeos.learning.post_trade import ensure_live_strategy
from tradeos.llm.client import LlmClient
from tradeos.logging_setup import setup_logging
from tradeos.memory.stores import MemoryStore
from tradeos.orchestration.monitor import PositionMonitor
from tradeos.orchestration.pipeline import OpportunityPipeline
from tradeos.orchestration.scheduler import Scheduler
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.chains import build_chain_providers
from tradeos.providers.chains.etherscan import (
    CHAIN_IDS, EtherscanProvider, EtherscanSwapSource)
from tradeos.providers.chains.helius import HeliusProvider
from tradeos.providers.dexscreener import DexScreenerProvider
from tradeos.risk.engine import RiskEngine
from tradeos.risk.killswitch import KillSwitch
from tradeos.strategies.scoring import ScoringConfig
from tradeos.wallets.registry import WalletRegistry
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
    scanners: list = None
    registry: WalletRegistry | None = None
    live_engine: LiveExecutionEngine | None = None


def build_app(settings: Settings | None = None) -> App:
    settings = settings or get_settings()
    setup_logging()

    # Demo mode enforcement: force paper trading
    if settings.demo_mode:
        logger.info("🟢 DEMO MODE ENABLED")
        logger.info("   Paper trading: YES (start balance: $%.2f)", settings.demo_starting_balance)
        logger.info("   Live trading: DISABLED (cannot be enabled)")
        logger.info("   Real signing: BLOCKED (no real transactions)")
        if settings.mode != Mode.PAPER:
            logger.warning("Demo mode requires TRADEOS_MODE=paper, overriding to paper")
            settings.mode = Mode.PAPER

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
        starting_balance = (settings.demo_starting_balance if settings.demo_mode
                           else settings.paper_starting_balance_usd)
        accounting.ensure_seeded(starting_balance)

    llm = LlmClient(settings)
    market = DexScreenerProvider()
    chain_providers = build_chain_providers()
    memory = MemoryStore(db)
    reputation = WalletReputationStore(db)
    registry = WalletRegistry(db)

    # Live execution wiring (Solana/Jupiter). Everything here is inert and
    # fail-closed until mode=live + confirm phrase + wallet + signer + RPC
    # all exist; the readiness endpoint reports exactly what is missing.
    signer = RemoteSignerClient()
    venue = JupiterVenue()
    zerox = ZeroExVenue()
    evm_executor = None
    if zerox.configured:
        evm_providers = {c: p for c, p in chain_providers.items()
                         if c in CHAIN_IDS}
        evm_executor = EvmLiveExecutor(settings, db, registry, signer, zerox,
                                       evm_providers, market, accounting)
        logger.info("0x venue configured: EVM live executor wired (still "
                    "gated by readiness)")
    live_engine = LiveExecutionEngine(
        settings, db, registry=registry, signer=signer, venue=venue,
        solana=chain_providers.get("solana"), market=market,
        accounting=accounting, evm=evm_executor)
    gateway = ExecutionGateway(settings, db, risk_engine, kill_switch,
                               accounting, live_engine=live_engine)

    scanners: list[SmartMoneyScanner] = []
    scanner = None  # the solana scanner, used by the webhook manager
    solana_provider = chain_providers.get("solana")
    if isinstance(solana_provider, HeliusProvider):
        scanner = SmartMoneyScanner(settings, db, solana_provider, reputation,
                                    chain="solana")
        scanners.append(scanner)
        logger.info("Helius indexer configured: solana smart-money scanner enabled")
    else:
        logger.info("no TRADEOS_HELIUS_API_KEY: solana smart-money scanner disabled")

    etherscan_key = os.environ.get("TRADEOS_ETHERSCAN_API_KEY")
    if etherscan_key:
        etherscan = EtherscanProvider(etherscan_key)
        evm_chains = [c for c in settings.allowed_chain_list if c in CHAIN_IDS]
        for chain in evm_chains:
            scanners.append(SmartMoneyScanner(
                settings, db, EtherscanSwapSource(etherscan, chain),
                reputation, chain=chain))
        logger.info("Etherscan indexer configured for: %s", ", ".join(evm_chains))
    else:
        logger.info("no TRADEOS_ETHERSCAN_API_KEY: EVM smart-money scanners disabled")

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
                          smartmoney_scanners=scanners,
                          webhook_manager=webhook_manager)

    return App(settings, db, kill_switch, risk_engine, accounting, gateway,
               market, pipeline, monitor, scheduler, llm, memory, reputation,
               agents, scanner, webhook_manager, scanners, registry, live_engine)
