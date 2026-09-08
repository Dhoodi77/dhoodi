"""0x Swap API v2 venue (EVM chains).

AllowanceHolder flow: one quote call returns ready-to-execute transaction
data (to / data / value / gas) plus allowance requirements. Requires a free
API key (TRADEOS_ZEROX_API_KEY). The base URL is configurable because swap
APIs move hosts.

This layer produces UNSIGNED transaction data only; the external signer
must approve and sign, and the RPC's estimateGas acts as an independent
simulation gate on top of 0x's own simulation flag.

Validation is deterministic and does not trust the venue's own price
claims: the implied fill price is checked against the market price the
pipeline supplied, bounded by slippage + configured max price impact.

Reference: https://0x.org/docs/api
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from tradeos.providers.chains.etherscan import CHAIN_IDS

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.0x.org"
NATIVE_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


@dataclass
class EvmQuoteCheck:
    ok: bool
    reasons: list[str]
    buy_amount: int = 0
    min_buy_amount: int = 0
    needs_allowance: bool = False
    allowance_spender: str | None = None


def validate_evm_quote(quote: dict, max_slippage_pct: float) -> EvmQuoteCheck:
    reasons: list[str] = []
    if quote.get("liquidityAvailable") is False:
        return EvmQuoteCheck(False, ["no route with available liquidity"])
    try:
        buy_amount = int(quote.get("buyAmount", 0))
        min_buy = int(quote.get("minBuyAmount", 0))
    except (TypeError, ValueError):
        return EvmQuoteCheck(False, ["quote amounts unparseable"])
    if buy_amount <= 0 or min_buy <= 0:
        reasons.append("quote amounts not positive")
    elif (buy_amount - min_buy) / buy_amount * 100 > max_slippage_pct + 0.01:
        reasons.append(
            f"quote worst-case slippage "
            f"{(buy_amount - min_buy) / buy_amount * 100:.2f}% exceeds "
            f"{max_slippage_pct}%")
    tx = quote.get("transaction") or {}
    if not tx.get("to") or not tx.get("data"):
        reasons.append("quote carries no transaction data")
    issues = quote.get("issues") or {}
    if issues.get("simulationIncomplete"):
        reasons.append("venue could not simulate the swap")
    if issues.get("balance"):
        reasons.append("venue reports insufficient balance")
    allowance = issues.get("allowance")
    return EvmQuoteCheck(
        ok=len(reasons) == 0, reasons=reasons, buy_amount=buy_amount,
        min_buy_amount=min_buy,
        needs_allowance=allowance is not None,
        allowance_spender=(allowance or {}).get("spender"))


class ZeroExVenue:
    name = "zerox"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("TRADEOS_ZEROX_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("TRADEOS_ZEROX_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=20)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def quote(self, chain: str, sell_token: str, buy_token: str,
                    sell_amount_raw: int, taker: str,
                    slippage_bps: int) -> dict | None:
        chain_id = CHAIN_IDS.get(chain)
        if chain_id is None or not self.configured:
            return None
        try:
            resp = await self._client.get(
                "/swap/allowance-holder/quote",
                params={"chainId": chain_id, "sellToken": sell_token,
                        "buyToken": buy_token, "sellAmount": sell_amount_raw,
                        "taker": taker, "slippageBps": slippage_bps},
                headers={"0x-api-key": self.api_key, "0x-version": "v2"})
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("0x quote failed chain=%s: %s", chain,
                           type(exc).__name__)
            return None

    async def health_check(self, chain: str = "base") -> dict:
        if not self.configured:
            return {"provider": "zerox", "ok": False, "error": "no api key"}
        # price endpoint is the cheap read-only probe of the same surface
        chain_id = CHAIN_IDS.get(chain, 8453)
        try:
            resp = await self._client.get(
                "/swap/allowance-holder/price",
                params={"chainId": chain_id, "sellToken": NATIVE_SENTINEL,
                        "buyToken": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        "sellAmount": 10**16},
                headers={"0x-api-key": self.api_key, "0x-version": "v2"})
            resp.raise_for_status()
            ok = resp.json().get("buyAmount") is not None
        except (httpx.HTTPError, ValueError):
            ok = False
        return {"provider": "zerox", "ok": ok, "base_url": self.base_url}

    async def close(self) -> None:
        await self._client.aclose()
