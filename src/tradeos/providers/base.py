"""Data provider abstraction.

Every external data source implements one of these interfaces so providers
can be added or swapped without touching strategy or agent code. PairData is
the normalized market-data unit the whole pipeline consumes.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field


@dataclass
class PairData:
    chain: str
    pair_address: str
    token_address: str
    symbol: str | None = None
    name: str | None = None
    dex: str | None = None
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    volume_5m: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_6h: float = 0.0
    price_change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    pair_created_at: float | None = None  # unix seconds
    captured_at: float = field(default_factory=time.time)
    raw: dict | None = None

    @property
    def age_hours(self) -> float | None:
        if not self.pair_created_at:
            return None
        return max(0.0, (self.captured_at - self.pair_created_at) / 3600)


class MarketDataProvider(abc.ABC):
    """DEX market data: pairs, prices, discovery."""

    name: str = "abstract"

    @abc.abstractmethod
    async def get_pair(self, chain: str, pair_address: str) -> PairData | None: ...

    @abc.abstractmethod
    async def get_token_pairs(self, chain: str, token_address: str) -> list[PairData]: ...

    @abc.abstractmethod
    async def discover(self) -> list[PairData]:
        """Return currently interesting pairs (trending / newly promoted)."""


class ChainProvider(abc.ABC):
    """On-chain data for one chain: transfers, holders, contracts.

    Implementations require an RPC/indexer endpoint configured per chain.
    """

    chain: str = "abstract"

    @abc.abstractmethod
    async def get_holder_summary(self, token_address: str) -> dict | None: ...

    @abc.abstractmethod
    async def get_recent_transfers(self, token_address: str, limit: int = 100) -> list[dict]: ...

    @abc.abstractmethod
    async def is_available(self) -> bool: ...
