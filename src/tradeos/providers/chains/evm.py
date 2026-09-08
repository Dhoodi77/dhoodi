"""Generic EVM JSON-RPC provider.

Real JSON-RPC calls (eth_blockNumber, eth_getLogs for ERC-20 Transfer
events). Holder distribution needs an indexer (covalent/moralis/etherscan
style); until one is configured this returns None rather than fabricated
data.
"""
from __future__ import annotations

import logging

import httpx

from tradeos.providers.base import ChainProvider

logger = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EvmProvider(ChainProvider):
    def __init__(self, chain: str, rpc_url: str | None):
        self.chain = chain
        self.rpc_url = rpc_url
        self._client = httpx.AsyncClient(timeout=15) if rpc_url else None
        self._req_id = 0

    async def _rpc(self, method: str, params: list) -> dict | None:
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
                logger.warning("%s rpc error: %s", self.chain, body["error"])
                return None
            return body.get("result")
        except httpx.HTTPError as exc:
            logger.warning("%s rpc failed: %s", self.chain, exc)
            return None

    async def is_available(self) -> bool:
        return await self._rpc("eth_blockNumber", []) is not None

    async def get_recent_transfers(self, token_address: str, limit: int = 100) -> list[dict]:
        head_hex = await self._rpc("eth_blockNumber", [])
        if head_hex is None:
            return []
        head = int(head_hex, 16)
        logs = await self._rpc("eth_getLogs", [{
            "fromBlock": hex(max(0, head - 2000)),
            "toBlock": hex(head),
            "address": token_address,
            "topics": [TRANSFER_TOPIC],
        }]) or []
        transfers = []
        for entry in logs[-limit:]:
            topics = entry.get("topics", [])
            if len(topics) >= 3:
                transfers.append({
                    "from": "0x" + topics[1][-40:],
                    "to": "0x" + topics[2][-40:],
                    "value_raw": int(entry.get("data", "0x0"), 16),
                    "block": int(entry.get("blockNumber", "0x0"), 16),
                    "tx_hash": entry.get("transactionHash"),
                })
        return transfers

    async def get_holder_summary(self, token_address: str) -> dict | None:
        # Requires an indexer API; not fabricated. Approximate signal from
        # recent transfer logs when RPC is available.
        transfers = await self.get_recent_transfers(token_address, limit=200)
        if not transfers:
            return None
        senders = {t["from"] for t in transfers}
        receivers = {t["to"] for t in transfers}
        return {
            "source": "rpc_transfer_logs",
            "recent_transfer_count": len(transfers),
            "unique_senders": len(senders),
            "unique_receivers": len(receivers),
            "note": "full holder distribution requires an indexer integration",
        }
