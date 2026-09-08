"""Execution gateway: the single choke point for all trade execution.

Order of authority (none of it is an LLM):
  1. Kill switch
  2. Instruction validation (pydantic + universe checks)
  3. Risk engine (policy, circuit breakers, per-trade limits)
  4. Mode routing: paper engine, or live engine (Stage 7, disabled)

Agents cannot call the paper/live engines directly; they only ever produce
decision objects that deterministic pipeline code converts into
TradeInstruction and submits here.
"""
from __future__ import annotations

import logging

from tradeos.config import Mode, Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.live import LiveExecutionEngine
from tradeos.execution.paper import ExecutionResult, PaperExecutionEngine
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.risk.engine import RiskEngine
from tradeos.risk.killswitch import KillSwitch

logger = logging.getLogger(__name__)


class ExecutionGateway:
    def __init__(self, settings: Settings, db: Database, risk_engine: RiskEngine,
                 kill_switch: KillSwitch, accounting: PortfolioAccounting):
        self.settings = settings
        self.db = db
        self.risk_engine = risk_engine
        self.kill_switch = kill_switch
        self.paper_engine = PaperExecutionEngine(settings, db, accounting)
        self.live_engine = LiveExecutionEngine(settings, db)
        self.accounting = accounting

    def submit(self, instr: TradeInstruction, market_price_usd: float) -> ExecutionResult:
        if self.kill_switch.is_active():
            self.db.audit("gateway", "blocked_kill_switch", instr.opportunity_id)
            return ExecutionResult(False, error="kill switch active")

        decision = self.risk_engine.evaluate_trade(instr, self.accounting.state())
        if not decision.approved:
            return ExecutionResult(
                False, error=f"risk rejected [{decision.rule}]: {'; '.join(decision.reasons)}"
            )

        if self.settings.mode == Mode.PAPER:
            return self.paper_engine.execute(instr, market_price_usd)
        if self.settings.mode == Mode.LIVE:
            return self.live_engine.execute(instr, market_price_usd)
        return ExecutionResult(False, error=f"mode {self.settings.mode.value} cannot execute")
