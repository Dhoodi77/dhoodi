"""Jupiter swap venue (Solana).

Quote and transaction building via Jupiter's public swap API — no key or
funds involved at this layer; it produces an UNSIGNED transaction that the
external signer must approve and sign. The base URL is configurable
(TRADEOS_JUPITER_BASE_URL) because Jupiter has moved hosts before; the
default is the current free endpoint.

Quote validation is deterministic and happens here, before anything is
built: slippage cap, price impact cap, mint match, and positive amounts.

Reference: https://dev.jup.ag/docs/swap-api
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://lite-api.jup.ag/swap/v1"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class QuoteCheck:
    ok: bool
    reasons: list[str]
    price_impact_pct: float | None = None
    out_amount: int | None = None
    min_out_amount: int | None = None


def validate_quote(quote: dict, input_mint: str, output_mint: str,
                   max_slippage_bps: int, max_price_impact_pct: float) -> QuoteCheck:
    reasons: list[str] = []
    if quote.get("inputMint") != input_mint:
        reasons.append("quote inputMint mismatch")
    if quote.get("outputMint") != output_mint:
        reasons.append("quote outputMint mismatch")
    try:
        in_amount = int(quote.get("inAmount", 0))
        out_amount = int(quote.get("outAmount", 0))
        min_out = int(quote.get("otherAmountThreshold", 0))
    except (TypeError, ValueError):
        return QuoteCheck(False, ["quote amounts unparseable"])
    if in_amount <= 0 or out_amount <= 0:
        reasons.append("quote amounts not positive")
    try:
        slippage_bps = int(quote.get("slippageBps", 10**9))
    except (TypeError, ValueError):
        slippage_bps = 10**9
    if slippage_bps > max_slippage_bps:
        reasons.append(f"quote slippage {slippage_bps}bps exceeds "
                       f"{max_slippage_bps}bps")
    impact = None
    try:
        impact = abs(float(quote.get("priceImpactPct", 0))) * 100
        if impact > max_price_impact_pct:
            reasons.append(f"price impact {impact:.2f}% exceeds "
                           f"{max_price_impact_pct}%")
    except (TypeError, ValueError):
        reasons.append("price impact unparseable")
    return QuoteCheck(len(reasons) == 0, reasons, impact, out_amount, min_out)


class JupiterVenue:
    name = "jupiter"
    chain = "solana"

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("TRADEOS_JUPITER_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=20)

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int,
                    slippage_bps: int) -> dict | None:
        try:
            resp = await self._client.get("/quote", params={
                "inputMint": input_mint, "outputMint": output_mint,
                "amount": amount_raw, "slippageBps": slippage_bps,
                "restrictIntermediateTokens": "true",
            })
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("jupiter quote failed: %s", type(exc).__name__)
            return None

    async def build_swap_transaction(self, quote: dict, user_public_key: str,
                                     max_priority_fee_lamports: int) -> str | None:
        """Returns the base64 UNSIGNED transaction, or None."""
        try:
            resp = await self._client.post("/swap", json={
                "quoteResponse": quote,
                "userPublicKey": user_public_key,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "priorityLevel": "high",
                        "maxLamports": max_priority_fee_lamports,
                    }
                },
            })
            resp.raise_for_status()
            tx = resp.json().get("swapTransaction")
            return tx if isinstance(tx, str) and tx else None
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("jupiter swap build failed: %s", type(exc).__name__)
            return None

    async def health_check(self) -> dict:
        quote = await self.quote(
            SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            LAMPORTS_PER_SOL, 100)
        ok = quote is not None and quote.get("outAmount") is not None
        return {"provider": "jupiter", "ok": ok, "base_url": self.base_url}

    async def close(self) -> None:
        await self._client.aclose()
