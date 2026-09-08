"""DexScreener provider.

Uses the public DexScreener HTTP API (no key). Documented limits: 300
requests/min for /latest/dex endpoints, 60/min for token-profiles and
token-boosts; a local token-bucket limiter stays below both. Retries with
exponential backoff on transient failures.

API reference: https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from tradeos.providers.base import MarketDataProvider, PairData

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.interval = 60.0 / max_per_minute
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = time.monotonic()


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_pair(p: dict) -> PairData | None:
    """Normalize one DexScreener pair object; tolerant of missing fields."""
    try:
        base = p.get("baseToken") or {}
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        txns = p.get("txns") or {}
        m5, h1 = txns.get("m5") or {}, txns.get("h1") or {}
        liq = p.get("liquidity") or {}
        created_ms = p.get("pairCreatedAt")
        return PairData(
            chain=str(p.get("chainId", "")).lower(),
            pair_address=p.get("pairAddress", ""),
            token_address=base.get("address", ""),
            symbol=base.get("symbol"),
            name=base.get("name"),
            dex=p.get("dexId"),
            price_usd=_num(p.get("priceUsd")),
            liquidity_usd=_num(liq.get("usd")),
            volume_24h=_num(vol.get("h24")),
            volume_1h=_num(vol.get("h1")),
            volume_5m=_num(vol.get("m5")),
            price_change_5m=_num(chg.get("m5")),
            price_change_1h=_num(chg.get("h1")),
            price_change_6h=_num(chg.get("h6")),
            price_change_24h=_num(chg.get("h24")),
            buys_5m=int(_num(m5.get("buys"))),
            sells_5m=int(_num(m5.get("sells"))),
            buys_1h=int(_num(h1.get("buys"))),
            sells_1h=int(_num(h1.get("sells"))),
            pair_created_at=(created_ms / 1000.0) if created_ms else None,
            raw=p,
        )
    except Exception:
        logger.exception("failed to parse DexScreener pair")
        return None


class DexScreenerProvider(MarketDataProvider):
    name = "dexscreener"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=15)
        self._dex_limiter = RateLimiter(240)      # under the 300/min limit
        self._profile_limiter = RateLimiter(50)   # under the 60/min limit

    async def _get(self, path: str, limiter: RateLimiter, retries: int = 3):
        for attempt in range(retries + 1):
            await limiter.wait()
            try:
                resp = await self._client.get(path)
                if resp.status_code == 429:
                    raise httpx.HTTPStatusError("rate limited", request=resp.request,
                                                response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == retries:
                    logger.warning("dexscreener request failed: %s %s", path, exc)
                    return None
                await asyncio.sleep(2 ** attempt)
        return None

    async def get_pair(self, chain: str, pair_address: str) -> PairData | None:
        data = await self._get(f"/latest/dex/pairs/{chain}/{pair_address}", self._dex_limiter)
        pairs = (data or {}).get("pairs") or (data or {}).get("pair") or []
        if isinstance(pairs, dict):
            pairs = [pairs]
        parsed = [parse_pair(p) for p in pairs]
        return next((p for p in parsed if p), None)

    async def get_token_pairs(self, chain: str, token_address: str) -> list[PairData]:
        data = await self._get(f"/tokens/v1/{chain}/{token_address}", self._dex_limiter)
        items = data if isinstance(data, list) else (data or {}).get("pairs") or []
        return [p for p in (parse_pair(i) for i in items) if p]

    async def search(self, query: str) -> list[PairData]:
        data = await self._get(f"/latest/dex/search?q={query}", self._dex_limiter)
        return [p for p in (parse_pair(i) for i in (data or {}).get("pairs") or []) if p]

    async def latest_token_profiles(self) -> list[dict]:
        data = await self._get("/token-profiles/latest/v1", self._profile_limiter)
        return data if isinstance(data, list) else []

    async def latest_boosted_tokens(self) -> list[dict]:
        data = await self._get("/token-boosts/latest/v1", self._profile_limiter)
        return data if isinstance(data, list) else []

    async def discover(self) -> list[PairData]:
        """Discovery: boosted + newly profiled tokens, resolved to their pairs."""
        found: dict[str, PairData] = {}
        candidates: list[tuple[str, str]] = []
        for item in (await self.latest_boosted_tokens())[:30]:
            chain, addr = item.get("chainId"), item.get("tokenAddress")
            if chain and addr:
                candidates.append((str(chain).lower(), addr))
        for item in (await self.latest_token_profiles())[:30]:
            chain, addr = item.get("chainId"), item.get("tokenAddress")
            if chain and addr:
                candidates.append((str(chain).lower(), addr))
        # de-dup, keep order
        seen: set[tuple[str, str]] = set()
        for chain, addr in candidates:
            if (chain, addr) in seen:
                continue
            seen.add((chain, addr))
            pairs = await self.get_token_pairs(chain, addr)
            if pairs:
                best = max(pairs, key=lambda p: p.liquidity_usd)
                found[f"{best.chain}:{best.pair_address}"] = best
        return list(found.values())

    async def close(self) -> None:
        await self._client.aclose()
