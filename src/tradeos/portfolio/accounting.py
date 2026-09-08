"""Portfolio accounting: cash ledger, positions, P&L.

All numbers derive from the database so a restart reconstructs identical
state. Daily P&L is realized P&L of positions closed since UTC midnight.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from tradeos.db.database import Database
from tradeos.risk.engine import PortfolioState


def utc_midnight_ts(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class PortfolioAccounting:
    def __init__(self, db: Database, mode: str):
        self.db = db
        self.mode = mode

    # --- cash -------------------------------------------------------------
    def ensure_seeded(self, starting_balance_usd: float) -> None:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM portfolio_ledger WHERE mode = ?", (self.mode,)
        )
        if row and row["n"] == 0:
            self.record_cash("deposit", starting_balance_usd, note="initial paper balance")

    def record_cash(self, kind: str, amount_usd: float, ref_trade_id: int | None = None,
                    note: str = "") -> None:
        self.db.execute(
            "INSERT INTO portfolio_ledger (mode, kind, amount_usd, ref_trade_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.mode, kind, amount_usd, ref_trade_id, note, time.time()),
        )

    def cash_usd(self) -> float:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(amount_usd), 0) AS c FROM portfolio_ledger WHERE mode = ?",
            (self.mode,),
        )
        return float(row["c"]) if row else 0.0

    # --- positions --------------------------------------------------------
    def open_positions(self) -> list[dict]:
        return self.db.query(
            "SELECT * FROM positions WHERE status = 'open' AND mode = ? ORDER BY opened_at DESC",
            (self.mode,),
        )

    def exposure_usd(self) -> float:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS e FROM positions "
            "WHERE status = 'open' AND mode = ?",
            (self.mode,),
        )
        return float(row["e"]) if row else 0.0

    def unrealized_pnl_usd(self) -> float:
        total = 0.0
        for pos in self.open_positions():
            last = pos["last_price_usd"] or pos["entry_price_usd"]
            total += (last - pos["entry_price_usd"]) * pos["quantity"]
        return total

    def realized_pnl_usd(self, since_ts: float = 0.0) -> float:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS p FROM positions "
            "WHERE status = 'closed' AND mode = ? AND closed_at >= ?",
            (self.mode, since_ts),
        )
        return float(row["p"]) if row else 0.0

    def daily_pnl_usd(self) -> float:
        return self.realized_pnl_usd(since_ts=utc_midnight_ts())

    def equity_usd(self) -> float:
        return self.cash_usd() + self.exposure_usd() + self.unrealized_pnl_usd()

    # --- peak equity for drawdown ----------------------------------------
    def update_peak_equity(self) -> float:
        equity = self.equity_usd()
        key = f"peak_equity_{self.mode}"
        peak = float(self.db.kv_get(key, "0") or 0)
        if equity > peak:
            peak = equity
            self.db.kv_set(key, str(peak))
        return peak

    # --- equity history ---------------------------------------------------
    def snapshot_equity(self, min_interval_s: float = 60.0) -> None:
        """Record an equity point for the balance-history chart, throttled so
        the monitor loop can call it every sweep."""
        last = self.db.query_one(
            "SELECT captured_at FROM equity_snapshots WHERE mode = ? "
            "ORDER BY captured_at DESC LIMIT 1", (self.mode,))
        now = time.time()
        if last and now - last["captured_at"] < min_interval_s:
            return
        self.db.execute(
            "INSERT INTO equity_snapshots (mode, equity_usd, cash_usd, "
            "exposure_usd, captured_at) VALUES (?, ?, ?, ?, ?)",
            (self.mode, self.equity_usd(), self.cash_usd(),
             self.exposure_usd(), now))
        # keep the table bounded (~20 days at 1/min)
        self.db.execute(
            "DELETE FROM equity_snapshots WHERE mode = ? AND captured_at < ?",
            (self.mode, now - 30 * 86400))

    def equity_history(self, hours: float = 24.0, max_points: int = 300) -> list[dict]:
        rows = self.db.query(
            "SELECT equity_usd, cash_usd, exposure_usd, captured_at "
            "FROM equity_snapshots WHERE mode = ? AND captured_at >= ? "
            "ORDER BY captured_at", (self.mode, time.time() - hours * 3600))
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]
        return rows

    def state(self) -> PortfolioState:
        peak = self.update_peak_equity()
        return PortfolioState(
            cash_usd=self.cash_usd(),
            open_positions=len(self.open_positions()),
            exposure_usd=self.exposure_usd(),
            daily_pnl_usd=self.daily_pnl_usd(),
            total_pnl_usd=self.realized_pnl_usd() + self.unrealized_pnl_usd(),
            peak_equity_usd=peak,
            equity_usd=self.equity_usd(),
        )
