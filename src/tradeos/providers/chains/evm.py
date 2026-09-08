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

    # --- transaction lifecycle (used by live execution) -----------------
    async def get_token_decimals(self, token_address: str) -> int | None:
        result = await self._rpc("eth_call", [
            {"to": token_address, "data": "0x313ce567"}, "latest"])  # decimals()
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            return None

    async def get_allowance(self, token: str, owner: str, spender: str) -> int | None:
        data = ("0xdd62ed3e"  # allowance(address,address)
                + owner.lower().replace("0x", "").rjust(64, "0")
                + spender.lower().replace("0x", "").rjust(64, "0"))
        result = await self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def approve_calldata(spender: str, amount: int) -> str:
        return ("0x095ea7b3"  # approve(address,uint256)
                + spender.lower().replace("0x", "").rjust(64, "0")
                + format(amount, "x").rjust(64, "0"))

    async def get_nonce(self, address: str) -> int | None:
        result = await self._rpc("eth_getTransactionCount", [address, "pending"])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            return None

    async def get_fees(self) -> dict | None:
        """EIP-1559 fee suggestion: base fee from the head block plus the
        node's priority-fee estimate, with headroom on the max fee."""
        block = await self._rpc("eth_getBlockByNumber", ["latest", False])
        try:
            base_fee = int(block["baseFeePerGas"], 16)
        except (TypeError, KeyError, ValueError):
            return None
        priority = await self._rpc("eth_maxPriorityFeePerGas", [])
        try:
            priority_fee = int(priority, 16)
        except (TypeError, ValueError):
            priority_fee = 1_500_000_000  # 1.5 gwei fallback
        return {"max_fee_per_gas": base_fee * 2 + priority_fee,
                "max_priority_fee_per_gas": priority_fee}

    async def estimate_gas(self, tx: dict) -> int | None:
        """Simulation gate: estimateGas executes the call and returns None
        when it would revert (or the RPC is unreachable) — fail closed."""
        params = {k: v for k, v in tx.items() if v is not None}
        result = await self._rpc("eth_estimateGas", [params])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            return None

    async def send_raw_transaction(self, raw_hex: str) -> str | None:
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        return await self._rpc("eth_sendRawTransaction", [raw_hex])

    async def get_receipt(self, tx_hash: str) -> dict | None:
        """None until mined; then {'ok': bool, 'gas_used', 'effective_gas_price',
        'logs'}."""
        receipt = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            return None
        try:
            return {
                "ok": int(receipt.get("status", "0x0"), 16) == 1,
                "gas_used": int(receipt.get("gasUsed", "0x0"), 16),
                "effective_gas_price": int(
                    receipt.get("effectiveGasPrice", "0x0"), 16),
                "logs": receipt.get("logs") or [],
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def token_delta_from_logs(logs: list[dict], token: str, wallet: str) -> int:
        """Net raw token amount transferred to/from the wallet in a receipt."""
        wallet_topic = "0x" + wallet.lower().replace("0x", "").rjust(64, "0")
        delta = 0
        for log in logs:
            if (log.get("address") or "").lower() != token.lower():
                continue
            topics = log.get("topics") or []
            if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                continue
            try:
                value = int(log.get("data", "0x0"), 16)
            except (TypeError, ValueError):
                continue
            if topics[2].lower() == wallet_topic:
                delta += value
            if topics[1].lower() == wallet_topic:
                delta -= value
        return delta

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
