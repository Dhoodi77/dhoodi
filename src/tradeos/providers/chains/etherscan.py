"""Etherscan V2 indexer for EVM wallet swap history.

One API key (TRADEOS_ETHERSCAN_API_KEY) covers all supported EVM chains via
the unified endpoint https://api.etherscan.io/v2/api?chainid=N.

Etherscan has no parsed-swap API, so swaps are reconstructed
deterministically per transaction hash from three account feeds:

  tokentx          ERC-20 transfers involving the wallet
  txlist           normal txs (native ETH/BNB/POL the wallet sent)
  txlistinternal   internal txs (native value delivered back to the wallet)

A hash counts as a swap only when it is unambiguous: exactly one distinct
non-counter token on one side, and exactly one counter asset (a registered
wrapped-native or stablecoin, or the native coin itself) on the other.
Multi-hop and multi-token transactions are skipped, never approximated.
Amounts stay denominated in their counter asset (counter_mint says which);
round-trip analysis matches buys to sells within the same counter, and
whale/USD logic only uses stablecoin-denominated rows.

Reference: https://docs.etherscan.io/etherscan-v2
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from tradeos.providers.swaps import SwapRecord

logger = logging.getLogger(__name__)

V2_URL = "https://api.etherscan.io/v2/api"

CHAIN_IDS = {
    "ethereum": 1,
    "base": 8453,
    "bsc": 56,
    "arbitrum": 42161,
    "polygon": 137,
}

NATIVE = "native"

# Wrapped-native counter assets per chain (lowercase).
WRAPPED_NATIVE = {
    "ethereum": {"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"},   # WETH
    "base": {"0x4200000000000000000000000000000000000006"},        # WETH
    "bsc": {"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"},         # WBNB
    "arbitrum": {"0x82af49447d8a07e3bd95bd0d56f35241523fbab1"},    # WETH
    "polygon": {"0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"},     # WPOL/WMATIC
}

# Stablecoin counter assets per chain (lowercase) — the only EVM rows the
# whale signal uses, because their USD value is unambiguous.
STABLE_COUNTERS = {
    "ethereum": {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    },
    "base": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    },
    "bsc": {
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    },
    "arbitrum": {
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
    },
    "polygon": {
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
        "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
    },
}


def counter_assets(chain: str) -> set[str]:
    return WRAPPED_NATIVE.get(chain, set()) | STABLE_COUNTERS.get(chain, set())


def _amount(row: dict) -> float:
    try:
        return float(row.get("value", 0)) / (10 ** int(row.get("tokenDecimal", 18)))
    except (TypeError, ValueError):
        return 0.0


def _wei(value) -> float:
    try:
        return float(value) / 1e18
    except (TypeError, ValueError):
        return 0.0


def parse_wallet_swaps(chain: str, wallet: str, tokentx: list[dict],
                       txlist: list[dict] | None = None,
                       internal: list[dict] | None = None) -> list[SwapRecord]:
    """Reconstruct unambiguous swaps for one wallet from Etherscan rows."""
    wallet_l = wallet.lower()
    counters = counter_assets(chain)

    native_out: dict[str, float] = {}   # hash -> native the wallet sent
    for tx in txlist or []:
        if tx.get("from", "").lower() == wallet_l and tx.get("isError") != "1":
            value = _wei(tx.get("value"))
            if value > 0:
                native_out[tx.get("hash", "")] = value
    native_in: dict[str, float] = {}    # hash -> native delivered to wallet
    for tx in internal or []:
        if tx.get("to", "").lower() == wallet_l and tx.get("isError") != "1":
            value = _wei(tx.get("value"))
            if value > 0:
                native_in[tx.get("hash", "")] = native_in.get(tx.get("hash", ""), 0) + value

    groups: dict[str, list[dict]] = {}
    for row in tokentx:
        tx_hash = row.get("hash")
        if tx_hash:
            groups.setdefault(tx_hash, []).append(row)

    records: list[SwapRecord] = []
    for tx_hash, rows in groups.items():
        token_in: dict[str, float] = {}      # non-counter tokens received
        token_out: dict[str, float] = {}
        counter_in: dict[str, float] = {}    # counter tokens received
        counter_out: dict[str, float] = {}
        ts = 0.0
        for row in rows:
            contract = row.get("contractAddress", "").lower()
            amount = _amount(row)
            ts = max(ts, float(row.get("timeStamp") or 0))
            if amount <= 0 or not contract:
                continue
            incoming = row.get("to", "").lower() == wallet_l
            outgoing = row.get("from", "").lower() == wallet_l
            if incoming == outgoing:
                continue  # self-transfer or unrelated leg
            bucket = (counter_in if incoming else counter_out) if contract in counters \
                else (token_in if incoming else token_out)
            bucket[contract] = bucket.get(contract, 0.0) + amount

        record = None
        if len(token_in) == 1 and not token_out:
            mint, amount = next(iter(token_in.items()))
            if len(counter_out) == 1 and not counter_in:
                counter, paid = next(iter(counter_out.items()))
                record = SwapRecord(chain, wallet_l, tx_hash, mint, "buy",
                                    amount, paid, counter, ts)
            elif not counter_out and not counter_in and tx_hash in native_out:
                record = SwapRecord(chain, wallet_l, tx_hash, mint, "buy",
                                    amount, native_out[tx_hash], NATIVE, ts)
        elif len(token_out) == 1 and not token_in:
            mint, amount = next(iter(token_out.items()))
            if len(counter_in) == 1 and not counter_out:
                counter, received = next(iter(counter_in.items()))
                record = SwapRecord(chain, wallet_l, tx_hash, mint, "sell",
                                    amount, received, counter, ts)
            elif not counter_in and not counter_out and tx_hash in native_in:
                record = SwapRecord(chain, wallet_l, tx_hash, mint, "sell",
                                    amount, native_in[tx_hash], NATIVE, ts)
        if record is not None:
            records.append(record)
    return records


class EtherscanProvider:
    """Shared across chains: one key, one rate limiter (free tier: 5 rps)."""

    name = "etherscan"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=20)
        self._min_interval = 0.25
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _get(self, chain: str, params: dict, retries: int = 3):
        chain_id = CHAIN_IDS.get(chain)
        if chain_id is None:
            return None
        query = {**params, "chainid": chain_id, "apikey": self._api_key}
        for attempt in range(retries + 1):
            await self._throttle()
            try:
                resp = await self._client.get(V2_URL, params=query)
                resp.raise_for_status()
                body = resp.json()
                # status "0" + "No transactions found" is a valid empty result;
                # any other status-0 (bad key, rate limit) is an error.
                if body.get("status") == "0" and \
                        "No transactions" not in str(body.get("message", "")):
                    raise ValueError(str(body.get("result", body.get("message"))))
                return body.get("result")
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == retries:
                    logger.warning("etherscan request failed chain=%s: %s",
                                   chain, type(exc).__name__)
                    return None
                await asyncio.sleep(2 ** attempt)
        return None

    async def _account_rows(self, chain: str, action: str, address: str,
                            limit: int) -> list[dict]:
        result = await self._get(chain, {
            "module": "account", "action": action, "address": address,
            "page": 1, "offset": min(limit, 100), "sort": "desc"})
        return result if isinstance(result, list) else []

    async def _account_rows_by_contract(self, chain: str, contract: str,
                                        limit: int) -> list[dict]:
        result = await self._get(chain, {
            "module": "account", "action": "tokentx", "contractaddress": contract,
            "page": 1, "offset": min(limit, 100), "sort": "desc"})
        return result if isinstance(result, list) else []

    async def get_wallet_swaps(self, chain: str, address: str,
                               limit: int = 100) -> list[SwapRecord]:
        tokentx = await self._account_rows(chain, "tokentx", address, limit)
        if not tokentx:
            return []
        txlist = await self._account_rows(chain, "txlist", address, limit)
        internal = await self._account_rows(chain, "txlistinternal", address, limit)
        return parse_wallet_swaps(chain, address, tokentx, txlist, internal)

    async def health_check(self, chain: str = "ethereum") -> dict:
        # account/balance is free-tier, cheap, and returns status="1" only
        # with a valid key — checks reachability, auth, and shape at once.
        result = await self._get(chain, {
            "module": "account", "action": "balance",
            "address": "0x000000000000000000000000000000000000dEaD",
            "tag": "latest"})
        ok = isinstance(result, str) and result.isdigit()
        return {"provider": "etherscan", "chain": chain, "ok": ok}

    async def close(self) -> None:
        await self._client.aclose()


class EtherscanSwapSource:
    """Per-chain adapter matching the scanner's swap-source interface."""

    def __init__(self, provider: EtherscanProvider, chain: str):
        self.provider = provider
        self.chain = chain

    async def get_address_swaps(self, address: str, limit: int = 100,
                                before: str | None = None) -> list[SwapRecord]:
        return await self.provider.get_wallet_swaps(self.chain, address, limit)

    async def token_activity(self, mint: str) -> tuple[list[SwapRecord], list[str]]:
        """Recently active wallets for a token. Etherscan has no parsed
        swaps per token, so this returns no records — just candidate
        wallets from recent transfers (pools and routers included; they
        wash out at analysis time because their histories don't parse into
        unambiguous round trips)."""
        rows = await self.provider._account_rows_by_contract(self.chain, mint, 100)
        skip = {"0x0000000000000000000000000000000000000000",
                "0x000000000000000000000000000000000000dead", mint.lower()}
        wallets: list[str] = []
        for row in rows:
            for side in ("to", "from"):
                addr = row.get(side, "").lower()
                if addr and addr not in skip and addr not in wallets:
                    wallets.append(addr)
        return [], wallets[:30]

    async def health_check(self) -> dict:
        return await self.provider.health_check(self.chain)
