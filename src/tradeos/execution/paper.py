"""Paper execution engine.

Runs the exact decision pipeline with simulated fills: fills at the quoted
market price worsened by a configurable simulated slippage, records the
trade, opens/closes the position, and moves cash through the ledger. No
network, no keys, no funds.
"""
from __future__ import annotations

import time

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.portfolio.accounting import PortfolioAccounting


class ExecutionResult:
    def __init__(self, ok: bool, trade_id: int | None = None,
                 position_id: int | None = None, error: str | None = None,
                 fill_price: float | None = None):
        self.ok = ok
        self.trade_id = trade_id
        self.position_id = position_id
        self.error = error
        self.fill_price = fill_price


class PaperExecutionEngine:
    mode = "paper"

    def __init__(self, settings: Settings, db: Database, accounting: PortfolioAccounting):
        self.settings = settings
        self.db = db
        self.accounting = accounting

    def execute(self, instr: TradeInstruction, market_price_usd: float) -> ExecutionResult:
        if market_price_usd <= 0:
            return self._fail(instr, "no market price available")
        if instr.side == "buy":
            return self._buy(instr, market_price_usd)
        return self._sell(instr, market_price_usd)

    # ------------------------------------------------------------------
    def _buy(self, instr: TradeInstruction, price: float) -> ExecutionResult:
        slip = self.settings.paper_simulated_slippage_pct / 100.0
        fill_price = price * (1 + slip)
        quantity = instr.amount_usd / fill_price
        now = time.time()

        trade_id = self.db.execute(
            "INSERT INTO trades (opportunity_id, chain, token_address, symbol, side, mode, "
            "requested_usd, filled_usd, price_usd, quantity, slippage_pct, gas_usd, status, "
            "created_at, filled_at) VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, 0, 'filled', ?, ?)",
            (instr.opportunity_id, instr.chain, instr.token_address, instr.symbol,
             self.mode, instr.amount_usd, instr.amount_usd, fill_price, quantity,
             self.settings.paper_simulated_slippage_pct, now, now),
        )
        position_id = self.db.execute(
            "INSERT INTO positions (opportunity_id, chain, token_address, pair_address, symbol, "
            "mode, status, entry_price_usd, quantity, cost_usd, stop_loss_pct, take_profit_pct, "
            "max_hold_hours, last_price_usd, opened_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (instr.opportunity_id, instr.chain, instr.token_address, instr.pair_address,
             instr.symbol, self.mode, fill_price, quantity, instr.amount_usd,
             self.settings.exit_stop_loss_pct, self.settings.exit_take_profit_pct,
             self.settings.exit_max_hold_hours, fill_price, now, now),
        )
        self.db.execute("UPDATE trades SET position_id = ? WHERE id = ?", (position_id, trade_id))
        self.accounting.record_cash("buy", -instr.amount_usd, trade_id,
                                    f"buy {instr.symbol or instr.token_address}")
        self.db.audit("execution_paper", "buy_filled", instr.opportunity_id,
                      {"trade_id": trade_id, "position_id": position_id,
                       "fill_price": fill_price, "quantity": quantity})
        return ExecutionResult(True, trade_id, position_id, fill_price=fill_price)

    def _sell(self, instr: TradeInstruction, price: float) -> ExecutionResult:
        if instr.position_id is None:
            return self._fail(instr, "sell requires position_id")
        pos = self.db.query_one(
            "SELECT * FROM positions WHERE id = ? AND status = 'open' AND mode = ?",
            (instr.position_id, self.mode),
        )
        if pos is None:
            return self._fail(instr, f"no open paper position {instr.position_id}")

        slip = self.settings.paper_simulated_slippage_pct / 100.0
        fill_price = price * (1 - slip)
        proceeds = fill_price * pos["quantity"]
        realized = proceeds - pos["cost_usd"]
        now = time.time()

        trade_id = self.db.execute(
            "INSERT INTO trades (opportunity_id, position_id, chain, token_address, symbol, "
            "side, mode, requested_usd, filled_usd, price_usd, quantity, slippage_pct, gas_usd, "
            "status, created_at, filled_at) "
            "VALUES (?, ?, ?, ?, ?, 'sell', ?, ?, ?, ?, ?, ?, 0, 'filled', ?, ?)",
            (instr.opportunity_id, pos["id"], pos["chain"], pos["token_address"], pos["symbol"],
             self.mode, proceeds, proceeds, fill_price, pos["quantity"],
             self.settings.paper_simulated_slippage_pct, now, now),
        )
        self.db.execute(
            "UPDATE positions SET status = 'closed', exit_price_usd = ?, exit_reason = ?, "
            "realized_pnl_usd = ?, last_price_usd = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (fill_price, instr.reason or "manual", realized, fill_price, now, now, pos["id"]),
        )
        self.accounting.record_cash("sell", proceeds, trade_id,
                                    f"sell {pos['symbol'] or pos['token_address']}")
        self.db.audit("execution_paper", "sell_filled", instr.opportunity_id,
                      {"trade_id": trade_id, "position_id": pos["id"],
                       "fill_price": fill_price, "realized_pnl_usd": realized,
                       "reason": instr.reason})
        return ExecutionResult(True, trade_id, pos["id"], fill_price=fill_price)

    def _fail(self, instr: TradeInstruction, error: str) -> ExecutionResult:
        self.db.execute(
            "INSERT INTO trades (opportunity_id, chain, token_address, symbol, side, mode, "
            "requested_usd, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?)",
            (instr.opportunity_id, instr.chain, instr.token_address, instr.symbol,
             instr.side, self.mode, instr.amount_usd, error, time.time()),
        )
        self.db.audit("execution_paper", "execution_failed", instr.opportunity_id,
                      {"error": error})
        return ExecutionResult(False, error=error)
