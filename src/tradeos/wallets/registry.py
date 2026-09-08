"""Wallet registry.

Wallets are registered by ADDRESS ONLY — this process never holds, stores,
or receives private keys. Signing happens in an external signer service
(see signer/README.md). Kinds:

  trading   may be used by the execution engine (funds it can lose)
  treasury  visible for accounting, execution refuses to use it
  tracked   third-party wallet under observation (smart money / whales)

The live engine only accepts an ACTIVE wallet of kind 'trading' on the
instruction's chain; everything else fails closed.
"""
from __future__ import annotations

import time

from tradeos.db.database import Database

KINDS = ("trading", "treasury", "tracked")


class WalletRegistry:
    def __init__(self, db: Database):
        self.db = db

    def register(self, label: str, chain: str, address: str,
                 kind: str = "trading", strategy: str | None = None,
                 risk_class: str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown wallet kind {kind!r}")
        if not label or not address or not chain:
            raise ValueError("label, chain, and address are required")
        wallet_id = self.db.execute(
            "INSERT INTO wallets (label, chain, address, kind, strategy, "
            "risk_class, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(chain, address) DO UPDATE SET label = excluded.label, "
            "kind = excluded.kind, strategy = excluded.strategy, "
            "risk_class = excluded.risk_class",
            (label, chain.lower(), address, kind, strategy, risk_class,
             time.time()))
        self.db.audit("wallet_registry", "wallet_registered",
                      detail={"label": label, "chain": chain, "address": address,
                              "kind": kind})
        return wallet_id

    def set_active(self, wallet_id: int, active: bool) -> None:
        self.db.execute("UPDATE wallets SET is_active = ? WHERE id = ?",
                        (1 if active else 0, wallet_id))
        self.db.audit("wallet_registry",
                      "wallet_activated" if active else "wallet_deactivated",
                      detail={"wallet_id": wallet_id})

    def get(self, wallet_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM wallets WHERE id = ?", (wallet_id,))

    def list(self, chain: str | None = None) -> list[dict]:
        if chain:
            return self.db.query(
                "SELECT * FROM wallets WHERE chain = ? ORDER BY id", (chain.lower(),))
        return self.db.query("SELECT * FROM wallets ORDER BY id")

    def trading_wallet(self, chain: str, wallet_id: int | None = None) -> dict | None:
        """The wallet execution is allowed to use: active, kind='trading',
        right chain. None means execution must refuse."""
        if wallet_id is not None:
            wallet = self.get(wallet_id)
            if wallet and wallet["is_active"] and wallet["kind"] == "trading" \
                    and wallet["chain"] == chain.lower():
                return wallet
            return None
        return self.db.query_one(
            "SELECT * FROM wallets WHERE chain = ? AND kind = 'trading' "
            "AND is_active = 1 ORDER BY id LIMIT 1", (chain.lower(),))
