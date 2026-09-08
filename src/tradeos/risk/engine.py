"""Deterministic risk engine.

This is the final gate before any execution, paper or live. It does not use
an LLM. Nothing an agent says can bypass it. Every decision is recorded as a
risk_event and in the audit log.

Checks, in order:
  1. kill switch
  2. policy present (all parameters configured)
  3. mode allows trading
  4. circuit breakers (daily loss, drawdown, emergency stop loss)
  5. per-trade limits (size, slippage, gas, chain, open-position count,
     portfolio exposure)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from tradeos.config import HARD_INITIAL_TRADE_CAP_USD, Mode, Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.risk.killswitch import KillSwitch
from tradeos.risk.policy import RiskPolicy


@dataclass
class PortfolioState:
    """Deterministic snapshot the engine judges against."""

    cash_usd: float = 0.0
    open_positions: int = 0
    exposure_usd: float = 0.0          # sum of open position cost basis
    daily_pnl_usd: float = 0.0         # realized today, negative = loss
    total_pnl_usd: float = 0.0
    peak_equity_usd: float = 0.0
    equity_usd: float = 0.0


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    rule: str | None = None            # first violated rule, if rejected

    @staticmethod
    def reject(rule: str, reason: str) -> "RiskDecision":
        return RiskDecision(approved=False, reasons=[reason], rule=rule)


class RiskEngine:
    def __init__(self, settings: Settings, db: Database, kill_switch: KillSwitch):
        self.settings = settings
        self.db = db
        self.kill_switch = kill_switch
        self.policy, self.policy_errors = RiskPolicy.from_config(settings.risk)

    # ------------------------------------------------------------------
    def trading_allowed(self) -> tuple[bool, list[str]]:
        problems: list[str] = []
        if self.kill_switch.is_active():
            problems.append("kill switch active")
        if self.policy is None:
            problems.extend(self.policy_errors)
        if self.settings.mode == Mode.DEVELOPMENT:
            problems.append("mode=development: trading disabled")
        if self.settings.mode == Mode.LIVE and \
                self.settings.live_trading_confirm != "I_UNDERSTAND_THE_RISKS":
            problems.append("live trading not confirmed (TRADEOS_LIVE_TRADING_CONFIRM)")
        return (len(problems) == 0), problems

    def check_circuit_breakers(self, state: PortfolioState) -> RiskDecision:
        assert self.policy is not None
        p = self.policy
        if state.daily_pnl_usd <= -p.max_daily_loss_usd:
            return RiskDecision.reject(
                "max_daily_loss",
                f"daily loss {state.daily_pnl_usd:.2f} breaches limit -{p.max_daily_loss_usd}",
            )
        if state.total_pnl_usd <= -p.emergency_stop_loss_usd:
            return RiskDecision.reject(
                "emergency_stop_loss",
                f"total loss {state.total_pnl_usd:.2f} breaches emergency stop "
                f"-{p.emergency_stop_loss_usd}",
            )
        if state.peak_equity_usd > 0:
            drawdown_pct = (state.peak_equity_usd - state.equity_usd) / state.peak_equity_usd * 100
            if drawdown_pct >= p.max_drawdown_pct:
                return RiskDecision.reject(
                    "max_drawdown",
                    f"drawdown {drawdown_pct:.1f}% breaches limit {p.max_drawdown_pct}%",
                )
        return RiskDecision(approved=True)

    def evaluate_trade(self, instr: TradeInstruction, state: PortfolioState) -> RiskDecision:
        """Full evaluation of a proposed entry. Records the outcome."""
        allowed, problems = self.trading_allowed()
        if not allowed:
            decision = RiskDecision(approved=False, reasons=problems, rule="trading_disabled")
            self._record(instr, decision)
            return decision
        assert self.policy is not None
        p = self.policy

        decision = self.check_circuit_breakers(state)
        if not decision.approved:
            # Circuit breaker trips activate the kill switch: a breached daily
            # loss or drawdown limit must stop the whole system, not one trade.
            self.kill_switch.activate("risk_engine", f"circuit breaker: {decision.rule}")
            self._record(instr, decision)
            return decision

        checks: list[tuple[bool, str, str]] = [
            (instr.side == "buy" or instr.side == "sell",
             "invalid_side", f"unknown side {instr.side!r}"),
            (instr.amount_usd > 0, "invalid_amount", "amount must be > 0"),
            (instr.amount_usd <= HARD_INITIAL_TRADE_CAP_USD or instr.side == "sell",
             "hard_initial_cap",
             f"buy of {instr.amount_usd} exceeds hard cap {HARD_INITIAL_TRADE_CAP_USD}"),
            (instr.amount_usd <= p.max_initial_position_usd or instr.side == "sell",
             "max_initial_position",
             f"buy of {instr.amount_usd} exceeds configured cap {p.max_initial_position_usd}"),
            (instr.max_slippage_pct <= p.max_slippage_pct,
             "max_slippage",
             f"slippage {instr.max_slippage_pct}% exceeds {p.max_slippage_pct}%"),
            (instr.max_gas_usd <= p.max_gas_usd,
             "max_gas", f"gas {instr.max_gas_usd} exceeds {p.max_gas_usd}"),
            (instr.chain in self.settings.allowed_chain_list,
             "chain_not_allowed", f"chain {instr.chain!r} not in allowed list"),
        ]
        if instr.side == "buy":
            checks += [
                (state.open_positions < p.max_open_positions,
                 "max_open_positions",
                 f"{state.open_positions} open positions at limit {p.max_open_positions}"),
                (state.exposure_usd + instr.amount_usd <= p.max_portfolio_exposure_usd,
                 "max_portfolio_exposure",
                 f"exposure {state.exposure_usd + instr.amount_usd:.2f} would exceed "
                 f"{p.max_portfolio_exposure_usd}"),
                (instr.amount_usd <= state.cash_usd,
                 "insufficient_cash",
                 f"amount {instr.amount_usd} exceeds cash {state.cash_usd:.2f}"),
            ]

        for ok, rule, reason in checks:
            if not ok:
                decision = RiskDecision.reject(rule, reason)
                self._record(instr, decision)
                return decision

        decision = RiskDecision(approved=True, reasons=["all risk checks passed"])
        self._record(instr, decision)
        return decision

    def _record(self, instr: TradeInstruction, decision: RiskDecision) -> None:
        self.db.execute(
            "INSERT INTO risk_events (opportunity_id, kind, rule, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                instr.opportunity_id,
                "approval" if decision.approved else "rejection",
                decision.rule,
                "; ".join(decision.reasons),
                time.time(),
            ),
        )
        self.db.audit(
            "risk_engine",
            "trade_approved" if decision.approved else "trade_rejected",
            instr.opportunity_id,
            {
                "side": instr.side, "amount_usd": instr.amount_usd,
                "chain": instr.chain, "token": instr.token_address,
                "rule": decision.rule, "reasons": decision.reasons,
            },
        )
