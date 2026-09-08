"""Position monitor: watches open positions and applies exit rules.

Exit rules are deterministic (stop-loss, take-profit, max hold time) and
run outside any LLM. Exits go through the same ExecutionGateway as entries;
after a close, the post-trade critic runs and its review is stored.
"""
from __future__ import annotations

import logging
import time

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.execution.gateway import ExecutionGateway
from tradeos.execution.instructions import TradeInstruction
from tradeos.learning.post_trade import run_post_trade_review
from tradeos.llm.client import LlmClient
from tradeos.providers.dexscreener import DexScreenerProvider

logger = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, settings: Settings, db: Database, market: DexScreenerProvider,
                 gateway: ExecutionGateway, llm: LlmClient):
        self.settings = settings
        self.db = db
        self.market = market
        self.gateway = gateway
        self.llm = llm

    def decide_exit(self, position: dict, price: float) -> str | None:
        """Deterministic exit decision. Returns exit reason or None."""
        entry = position["entry_price_usd"]
        if entry <= 0 or price <= 0:
            return None
        change_pct = (price - entry) / entry * 100
        if change_pct <= -(position["stop_loss_pct"] or self.settings.exit_stop_loss_pct):
            return "stop_loss"
        if change_pct >= (position["take_profit_pct"] or self.settings.exit_take_profit_pct):
            return "take_profit"
        max_hold = position["max_hold_hours"] or self.settings.exit_max_hold_hours
        if (time.time() - position["opened_at"]) / 3600 >= max_hold:
            return "max_hold"
        return None

    async def check_all(self) -> int:
        """One monitoring sweep. Returns the number of positions closed."""
        closed = 0
        # Filter by the accounting mode, not the raw settings mode: development
        # mode books everything as 'paper' and the two must never diverge.
        positions = self.db.query(
            "SELECT * FROM positions WHERE status = 'open' AND mode = ?",
            (self.gateway.accounting.mode,))
        for pos in positions:
            price = None
            if pos["pair_address"]:
                pair = await self.market.get_pair(pos["chain"], pos["pair_address"])
                price = pair.price_usd if pair else None
            if price is None:
                pairs = await self.market.get_token_pairs(pos["chain"], pos["token_address"])
                price = max(pairs, key=lambda p: p.liquidity_usd).price_usd if pairs else None
            if price is None or price <= 0:
                logger.warning("no price for position %s; skipping sweep", pos["id"])
                continue

            self.db.execute(
                "UPDATE positions SET last_price_usd = ?, updated_at = ? WHERE id = ?",
                (price, time.time(), pos["id"]))

            reason = self.decide_exit(pos, price)
            if reason:
                closed += await self._close(pos, price, reason)
        return closed

    async def _close(self, pos: dict, price: float, reason: str) -> int:
        instr = TradeInstruction(
            opportunity_id=pos["opportunity_id"] or "manual",
            chain=pos["chain"], token_address=pos["token_address"],
            pair_address=pos["pair_address"], symbol=pos["symbol"],
            side="sell", amount_usd=max(pos["cost_usd"], 0.01),
            max_slippage_pct=self.gateway.risk_engine.policy.max_slippage_pct
            if self.gateway.risk_engine.policy else 1.0,
            max_gas_usd=self.gateway.risk_engine.policy.max_gas_usd
            if self.gateway.risk_engine.policy else 1.0,
            position_id=pos["id"], reason=reason,
        )
        result = self.gateway.submit(instr, price)
        if not result.ok:
            self.db.alert("critical", f"EXIT FAILED for {pos['symbol']}",
                          f"position {pos['id']}: {result.error}")
            return 0
        closed_pos = self.db.query_one("SELECT * FROM positions WHERE id = ?", (pos["id"],))
        pnl = closed_pos["realized_pnl_usd"] if closed_pos else None
        self.db.alert("high", f"Closed {pos['symbol']} ({reason})",
                      f"P&L: ${pnl:.2f}" if pnl is not None else "")
        if closed_pos:
            try:
                await run_post_trade_review(self.db, self.llm, closed_pos)
            except Exception:
                logger.exception("post-trade review failed for position %s", pos["id"])
        return 1
