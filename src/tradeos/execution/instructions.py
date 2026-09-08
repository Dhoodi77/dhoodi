"""Trade instruction: the only object the execution layer accepts.

Constructed by deterministic code from an approved decision object; never
built directly from natural language. Validated with pydantic so malformed
agent output can never reach execution.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field, field_validator


class TradeInstruction(BaseModel):
    opportunity_id: str
    chain: str
    token_address: str
    pair_address: str | None = None
    symbol: str | None = None
    side: str  # buy | sell
    amount_usd: float = Field(gt=0)
    max_slippage_pct: float = Field(gt=0)
    max_gas_usd: float = Field(gt=0)
    wallet_id: int | None = None
    position_id: int | None = None  # required for sells
    reason: str = ""
    created_at: float = Field(default_factory=time.time)

    @field_validator("side")
    @classmethod
    def _side(cls, v: str) -> str:
        if v not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return v

    @field_validator("chain")
    @classmethod
    def _chain(cls, v: str) -> str:
        return v.strip().lower()
