"""Thread-safe SQLite access layer.

SQLite in WAL mode with a single connection per Database instance guarded by
a re-entrant lock. This process is the only writer, which is exactly the
deployment model (single supervised process on a VPS). Keeping raw SQL here
rather than an ORM makes every financially relevant write explicit and
auditable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = "1"


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_PATH.read_text())
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    # --- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement; returns lastrowid."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        """Execute several writes atomically."""
        with self._lock:
            try:
                for sql, params in statements:
                    self._conn.execute(sql, tuple(params))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- kv state --------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )

    # --- audit -----------------------------------------------------------
    def audit(self, actor: str, action: str, opportunity_id: str | None = None,
              detail: dict | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log (actor, action, opportunity_id, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, opportunity_id, json.dumps(detail or {}, default=str), time.time()),
        )

    def system_event(self, kind: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO system_events (kind, detail, created_at) VALUES (?, ?, ?)",
            (kind, detail, time.time()),
        )

    def alert(self, priority: str, title: str, body: str = "") -> None:
        self.execute(
            "INSERT INTO alerts (priority, title, body, created_at) VALUES (?, ?, ?, ?)",
            (priority, title, body, time.time()),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
