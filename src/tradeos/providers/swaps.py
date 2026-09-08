"""Normalized swap record shared by all swap-history providers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SwapRecord:
    chain: str
    wallet: str
    signature: str            # tx signature / hash
    token_mint: str           # the traded token (never the counter asset)
    direction: str            # buy | sell (of token_mint)
    token_amount: float
    sol_amount: float         # counter-asset amount paid/received (SOL on
                              # solana; WETH/stable/native units on EVM —
                              # the column name is historical, the unit is
                              # defined by counter_mint)
    counter_mint: str
    block_time: float
