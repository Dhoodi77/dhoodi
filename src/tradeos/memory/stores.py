"""Layered memory: user, market, trading, agent.

Backed by the memories table. Secrets are never stored here — the write
path runs the same redaction used for logs as defense in depth.
"""
from __future__ import annotations

import time

from tradeos.db.database import Database
from tradeos.logging_setup import redact

LAYERS = ("user", "market", "trading", "agent")


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def remember(self, layer: str, key: str, content: str) -> None:
        if layer not in LAYERS:
            raise ValueError(f"unknown memory layer {layer!r}")
        self.db.execute(
            "INSERT INTO memories (layer, key, content, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(layer, key) DO UPDATE SET content = excluded.content, "
            "created_at = excluded.created_at",
            (layer, key, redact(content), time.time()),
        )

    def recall(self, layer: str, key: str) -> str | None:
        row = self.db.query_one(
            "SELECT content FROM memories WHERE layer = ? AND key = ?", (layer, key))
        return row["content"] if row else None

    def recall_layer(self, layer: str, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT key, content, created_at FROM memories WHERE layer = ? "
            "ORDER BY created_at DESC LIMIT ?", (layer, limit))

    def forget(self, layer: str, key: str) -> None:
        self.db.execute("DELETE FROM memories WHERE layer = ? AND key = ?", (layer, key))
