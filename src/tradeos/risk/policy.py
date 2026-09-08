"""Risk policy: validated, deterministic, non-bypassable.

A RiskPolicy only exists if every parameter is explicitly configured. If any
is missing, from_config returns errors and NO trading of any kind is
permitted. The $20 hard initial-entry cap is enforced at construction time:
configuration can lower it but can never raise it above
HARD_INITIAL_TRADE_CAP_USD.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradeos.config import HARD_INITIAL_TRADE_CAP_USD, RiskConfig

REQUIRED_FIELDS = [
    "max_initial_position_usd",
    "max_position_usd",
    "max_daily_loss_usd",
    "max_portfolio_exposure_usd",
    "max_slippage_pct",
    "max_gas_usd",
    "max_open_positions",
    "max_drawdown_pct",
    "emergency_stop_loss_usd",
]


@dataclass(frozen=True)
class RiskPolicy:
    max_initial_position_usd: float
    max_position_usd: float
    max_daily_loss_usd: float
    max_portfolio_exposure_usd: float
    max_slippage_pct: float
    max_gas_usd: float
    max_open_positions: int
    max_drawdown_pct: float
    emergency_stop_loss_usd: float

    @staticmethod
    def from_config(cfg: RiskConfig) -> tuple["RiskPolicy | None", list[str]]:
        errors: list[str] = []
        for field in REQUIRED_FIELDS:
            value = getattr(cfg, field)
            if value is None:
                errors.append(f"risk parameter not configured: {field}")
            elif value <= 0:
                errors.append(f"risk parameter must be > 0: {field}={value}")
        if errors:
            return None, errors

        initial_cap = min(float(cfg.max_initial_position_usd), HARD_INITIAL_TRADE_CAP_USD)
        return (
            RiskPolicy(
                max_initial_position_usd=initial_cap,
                max_position_usd=float(cfg.max_position_usd),
                max_daily_loss_usd=float(cfg.max_daily_loss_usd),
                max_portfolio_exposure_usd=float(cfg.max_portfolio_exposure_usd),
                max_slippage_pct=float(cfg.max_slippage_pct),
                max_gas_usd=float(cfg.max_gas_usd),
                max_open_positions=int(cfg.max_open_positions),
                max_drawdown_pct=float(cfg.max_drawdown_pct),
                emergency_stop_loss_usd=float(cfg.emergency_stop_loss_usd),
            ),
            [],
        )
