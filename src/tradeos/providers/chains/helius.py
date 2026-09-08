"""Helius indexer provider for Solana.

Requires TRADEOS_HELIUS_API_KEY. Two Helius surfaces are used:

  - RPC + DAS at https://mainnet.helius-rpc.com/?api-key=KEY
    (standard Solana RPC plus getTokenAccounts for full holder distribution)
  - Enhanced Transactions API at https://api.helius.xyz/v0
    (/addresses/{address}/transactions?type=SWAP returns human-parsed swaps)

Only SOL<->token swaps parsed from Helius's own `events.swap` structure are
recorded; ambiguous transactions are skipped, never guessed. The API key
travels only in the query string of requests and is covered by the log
redaction filter (api-key=... pattern).

Reference: https://docs.helius.dev
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from tradeos.providers.chains.solana import SolanaProvider

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
RPC_BASE = "https://mainnet.helius-rpc.com/"
API_BASE = "https://api.helius.xyz"


@dataclass
class SwapRecord:
    chain: str
    wallet: str
    signature: str
    token_mint: str
    direction: str            # buy | sell (of token_mint, against SOL)
    token_amount: float
    sol_amount: float
    counter_mint: str
    block_time: float


def _raw_amount(entry: dict) -> float:
    raw = entry.get("rawTokenAmount") or {}
    try:
        return float(raw.get("tokenAmount", 0)) / (10 ** int(raw.get("decimals", 0)))
    except (TypeError, ValueError):
        return 0.0


def _lamports(value) -> float:
    try:
        return float(value) / 1e9
    except (TypeError, ValueError):
        return 0.0


def parse_enhanced_swap(tx: dict) -> SwapRecord | None:
    """Parse one Helius enhanced transaction into a SOL<->token SwapRecord.

    Returns None for anything that is not an unambiguous SOL (or wSOL) to
    single-token swap: token-token routes, multi-token swaps, and
    transactions without a parsed swap event are skipped rather than
    approximated.
    """
    try:
        if tx.get("type") != "SWAP":
            return None
        wallet = tx.get("feePayer")
        signature = tx.get("signature")
        if not wallet or not signature:
            return None
        ev = (tx.get("events") or {}).get("swap") or {}

        token_inputs = [t for t in (ev.get("tokenInputs") or [])
                        if t.get("mint") and t["mint"] != WSOL_MINT]
        token_outputs = [t for t in (ev.get("tokenOutputs") or [])
                         if t.get("mint") and t["mint"] != WSOL_MINT]
        sol_in = _lamports((ev.get("nativeInput") or {}).get("amount")) + sum(
            _raw_amount(t) for t in (ev.get("tokenInputs") or [])
            if t.get("mint") == WSOL_MINT)
        sol_out = _lamports((ev.get("nativeOutput") or {}).get("amount")) + sum(
            _raw_amount(t) for t in (ev.get("tokenOutputs") or [])
            if t.get("mint") == WSOL_MINT)

        block_time = float(tx.get("timestamp") or 0)

        if sol_in > 0 and len(token_outputs) == 1 and not token_inputs:
            token = token_outputs[0]
            return SwapRecord("solana", wallet, signature, token["mint"], "buy",
                              _raw_amount(token), sol_in, WSOL_MINT, block_time)
        if sol_out > 0 and len(token_inputs) == 1 and not token_outputs:
            token = token_inputs[0]
            return SwapRecord("solana", wallet, signature, token["mint"], "sell",
                              _raw_amount(token), sol_out, WSOL_MINT, block_time)
        return None
    except Exception:
        logger.exception("failed to parse enhanced swap")
        return None


class HeliusProvider(SolanaProvider):
    """SolanaProvider (RPC methods inherited) + DAS holders + parsed swaps."""

    name = "helius"

    def __init__(self, api_key: str):
        super().__init__(rpc_url=f"{RPC_BASE}?api-key={api_key}")
        self._api_key = api_key
        self._api_client = httpx.AsyncClient(base_url=API_BASE, timeout=20)
        # Helius free tier allows 10 RPS; stay well under it.
        self._min_interval = 0.25
        self._last_call = 0.0
        self._call_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._call_lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _get_json(self, path: str, params: dict, retries: int = 3):
        for attempt in range(retries + 1):
            await self._throttle()
            try:
                resp = await self._api_client.get(
                    path, params={**params, "api-key": self._api_key})
                if resp.status_code == 429:
                    raise httpx.HTTPStatusError("rate limited",
                                                request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == retries:
                    logger.warning("helius request failed: %s %s", path,
                                   type(exc).__name__)
                    return None
                await asyncio.sleep(2 ** attempt)
        return None

    # --- swaps ---------------------------------------------------------
    async def get_address_swaps(self, address: str, limit: int = 100,
                                before: str | None = None) -> list[SwapRecord]:
        """Parsed SOL<->token swaps for any address (wallet or mint)."""
        params: dict = {"type": "SWAP", "limit": min(limit, 100)}
        if before:
            params["before"] = before
        data = await self._get_json(f"/v0/addresses/{address}/transactions", params)
        if not isinstance(data, list):
            return []
        return [r for r in (parse_enhanced_swap(tx) for tx in data) if r]

    # --- holders -------------------------------------------------------
    async def get_holder_summary(self, token_address: str) -> dict | None:
        """Full holder distribution via DAS getTokenAccounts (paginated,
        capped at 4000 accounts); falls back to the base RPC largest-accounts
        approximation."""
        balances: dict[str, int] = {}
        truncated = False
        for page in range(1, 5):
            result = await self._rpc("getTokenAccounts", {
                "mint": token_address, "limit": 1000, "page": page})
            accounts = (result or {}).get("token_accounts") or []
            for acct in accounts:
                owner = acct.get("owner")
                try:
                    amount = int(acct.get("amount", 0))
                except (TypeError, ValueError):
                    amount = 0
                if owner and amount > 0:
                    balances[owner] = balances.get(owner, 0) + amount
            if len(accounts) < 1000:
                break
        else:
            truncated = True

        if not balances:
            return await super().get_holder_summary(token_address)

        total = sum(balances.values())
        top = sorted(balances.values(), reverse=True)
        return {
            "source": "helius_das",
            "holder_count": len(balances),
            "top10_holder_pct": round(sum(top[:10]) / total * 100, 2),
            "top20_holder_pct": round(sum(top[:20]) / total * 100, 2),
            "truncated": truncated,
        }

    async def close(self) -> None:
        await self._api_client.aclose()
        if self._client:
            await self._client.aclose()
