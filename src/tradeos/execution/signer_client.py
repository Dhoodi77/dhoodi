"""Client for the external signer service.

The signer is a SEPARATE process (see signer/) that holds the private key
and applies its own independent policy before signing. This process only
ever sends an unsigned transaction plus intent metadata and receives back
either a signed transaction or a refusal. Key material never crosses this
boundary in either direction.

Configuration: TRADEOS_SIGNER_URL, TRADEOS_SIGNER_TOKEN.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class SignerRefused(Exception):
    """The signer's own policy rejected the transaction."""


class RemoteSignerClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = (url or os.environ.get("TRADEOS_SIGNER_URL") or "").rstrip("/")
        self.token = token or os.environ.get("TRADEOS_SIGNER_TOKEN") or ""
        self._client = httpx.AsyncClient(timeout=15) if self.url else None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def health(self) -> bool:
        if not self._client:
            return False
        try:
            resp = await self._client.get(f"{self.url}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def sign(self, transaction_b64: str, chain: str, wallet_address: str,
                   intent: dict) -> str:
        """Returns the signed transaction (base64). Raises SignerRefused on a
        policy refusal, RuntimeError on transport failure — callers treat
        both as a failed, un-submitted trade."""
        if not self._client or not self.configured:
            raise RuntimeError("signer not configured")
        try:
            resp = await self._client.post(
                f"{self.url}/sign",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"transaction": transaction_b64, "chain": chain,
                      "wallet_address": wallet_address, "intent": intent})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"signer unreachable: {type(exc).__name__}")
        if resp.status_code == 403:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except ValueError:
                pass
            raise SignerRefused(detail or "signer policy refused")
        if resp.status_code != 200:
            raise RuntimeError(f"signer error http {resp.status_code}")
        signed = resp.json().get("signed_transaction")
        if not isinstance(signed, str) or not signed:
            raise RuntimeError("signer returned no transaction")
        return signed

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
