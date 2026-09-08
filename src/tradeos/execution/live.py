"""Live execution engine — Stage 7, deliberately not implemented.

This is a real integration point, not a fake one: it refuses to run rather
than pretending to trade. Enabling live execution requires, at minimum:

  - TRADEOS_LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS
  - a registered trading wallet with an external signer (keys must NEVER
    enter this process's config, logs, or database)
  - per-chain RPC endpoints (TRADEOS_RPC_<CHAIN>)
  - a DEX router integration (e.g. Jupiter for Solana, 0x/1inch for EVM)
    with transaction simulation before submission

Until all of that exists and Stages 1-6 pass their tests, every call fails
closed and is audited.
"""
from __future__ import annotations

import time

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.paper import ExecutionResult


class LiveExecutionEngine:
    mode = "live"

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def execute(self, instr: TradeInstruction, market_price_usd: float) -> ExecutionResult:
        error = ("live execution not implemented: Stage 7 requires wallet signer, "
                 "RPC endpoints, and DEX router integration")
        self.db.execute(
            "INSERT INTO trades (opportunity_id, chain, token_address, symbol, side, mode, "
            "requested_usd, status, error, created_at) VALUES (?, ?, ?, ?, ?, 'live', ?, "
            "'failed', ?, ?)",
            (instr.opportunity_id, instr.chain, instr.token_address, instr.symbol,
             instr.side, instr.amount_usd, error, time.time()),
        )
        self.db.audit("execution_live", "refused_not_implemented", instr.opportunity_id)
        return ExecutionResult(False, error=error)
