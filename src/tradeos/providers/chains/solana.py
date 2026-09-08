"""Solana JSON-RPC provider.

Real RPC calls against a configured endpoint (getTokenLargestAccounts,
getTokenSupply, getSignaturesForAddress). Returns None/[] when no endpoint
is configured — never fabricated data.
"""
from __future__ import annotations

import logging

import httpx

from tradeos.providers.base import ChainProvider

logger = logging.getLogger(__name__)


class SolanaProvider(ChainProvider):
    chain = "solana"

    def __init__(self, rpc_url: str | None):
        self.rpc_url = rpc_url
        self._client = httpx.AsyncClient(timeout=15) if rpc_url else None
        self._req_id = 0

    async def _rpc(self, method: str, params: list):
        if not self._client or not self.rpc_url:
            return None
        self._req_id += 1
        try:
            resp = await self._client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": self._req_id,
                      "method": method, "params": params},
            )
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                logger.warning("solana rpc error: %s", body["error"])
                return None
            return body.get("result")
        except httpx.HTTPError as exc:
            logger.warning("solana rpc failed: %s", exc)
            return None

    async def is_available(self) -> bool:
        return await self._rpc("getSlot", []) is not None

    async def get_holder_summary(self, token_address: str) -> dict | None:
        largest = await self._rpc("getTokenLargestAccounts", [token_address])
        supply = await self._rpc("getTokenSupply", [token_address])
        if not largest or not supply:
            return None
        total = float(supply["value"].get("uiAmount") or 0)
        top = [float(a.get("uiAmount") or 0) for a in largest.get("value", [])]
        top10_share = (sum(top[:10]) / total * 100) if total > 0 else None
        return {
            "source": "solana_rpc",
            "total_supply": total,
            "top10_holder_pct": top10_share,
            "largest_accounts": len(top),
        }

    async def get_recent_transfers(self, token_address: str, limit: int = 100) -> list[dict]:
        sigs = await self._rpc(
            "getSignaturesForAddress", [token_address, {"limit": min(limit, 100)}]
        ) or []
        return [{"tx_hash": s.get("signature"), "slot": s.get("slot"),
                 "err": s.get("err")} for s in sigs]

    # --- transaction lifecycle (used by live execution) -----------------
    async def get_token_decimals(self, mint: str) -> int | None:
        supply = await self._rpc("getTokenSupply", [mint])
        try:
            return int(supply["value"]["decimals"])
        except (TypeError, KeyError, ValueError):
            return None

    async def simulate_transaction(self, tx_b64: str) -> dict:
        """Pre-submit simulation. Returns {'ok': bool, 'error': ..., 'logs': ...}.
        A failed RPC call counts as a failed simulation — fail closed."""
        result = await self._rpc("simulateTransaction", [
            tx_b64, {"encoding": "base64", "commitment": "processed",
                     "replaceRecentBlockhash": True}])
        if result is None:
            return {"ok": False, "error": "simulation rpc unavailable"}
        value = result.get("value") or {}
        err = value.get("err")
        return {"ok": err is None, "error": err,
                "logs": (value.get("logs") or [])[-10:],
                "units_consumed": value.get("unitsConsumed")}

    async def send_transaction(self, tx_b64: str) -> str | None:
        """Submit a signed transaction. Returns the signature or None."""
        return await self._rpc("sendTransaction", [
            tx_b64, {"encoding": "base64", "skipPreflight": False,
                     "maxRetries": 3}])

    async def get_signature_status(self, signature: str) -> dict | None:
        result = await self._rpc("getSignatureStatuses",
                                 [[signature], {"searchTransactionHistory": True}])
        try:
            return (result or {}).get("value", [None])[0]
        except (AttributeError, IndexError):
            return None

    async def get_transaction_balances(self, signature: str) -> dict | None:
        """Pre/post balances for fill reconciliation; None if unavailable."""
        result = await self._rpc("getTransaction", [
            signature, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed"}])
        meta = (result or {}).get("meta")
        if not meta:
            return None
        return {
            "err": meta.get("err"),
            "fee_lamports": meta.get("fee"),
            "pre_token": meta.get("preTokenBalances") or [],
            "post_token": meta.get("postTokenBalances") or [],
            "pre_sol": meta.get("preBalances") or [],
            "post_sol": meta.get("postBalances") or [],
            "account_keys": ((result or {}).get("transaction") or {})
            .get("message", {}).get("accountKeys") or [],
        }
