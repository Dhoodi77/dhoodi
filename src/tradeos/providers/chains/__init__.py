"""Chain providers.

Each supported chain gets a ChainProvider. EVM chains (ethereum, base, bsc,
arbitrum, polygon) share one JSON-RPC implementation parameterized by RPC
URL; Solana has its own. Providers require a configured RPC endpoint
(TRADEOS_RPC_<CHAIN> env var); without one they report unavailable and the
on-chain agent degrades gracefully instead of inventing data.
"""
from __future__ import annotations

import os

from tradeos.providers.chains.evm import EvmProvider
from tradeos.providers.chains.solana import SolanaProvider

EVM_CHAINS = ["ethereum", "base", "bsc", "arbitrum", "polygon"]
SUPPORTED_CHAINS = EVM_CHAINS + ["solana"]


def build_chain_providers() -> dict[str, object]:
    providers: dict[str, object] = {}
    for chain in EVM_CHAINS:
        rpc = os.environ.get(f"TRADEOS_RPC_{chain.upper()}")
        providers[chain] = EvmProvider(chain, rpc)
    providers["solana"] = SolanaProvider(os.environ.get("TRADEOS_RPC_SOLANA"))
    return providers
