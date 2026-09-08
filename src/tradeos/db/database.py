"""Thread-safe database access layer.

Supports both SQLite (development) and PostgreSQL (production).

SQLite runs in WAL mode. PostgreSQL uses connection pooling.
Single connection per Database instance guarded by a re-entrant lock.
Keeping raw SQL here rather than an ORM makes every financially relevant write explicit and
auditable.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = "1"


class Database:
    def __init__(self, path: str):
        self.path = path
        self._is_postgres = path.startswith("postgresql://") or path.startswith("postgres://")
        self._lock = threading.RLock()

        if self._is_postgres:
            self._init_postgres(path)
        else:
            self._init_sqlite(path)

        self._migrate()

    def _init_sqlite(self, path: str) -> None:
        """Initialize SQLite connection."""
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        logger.info("SQLite database initialized: %s", path)

    def _init_postgres(self, connection_string: str) -> None:
        """Initialize PostgreSQL connection."""
        try:
            import psycopg2
            import psycopg2.pool
        except ImportError:
            raise RuntimeError(
                "PostgreSQL support requires psycopg2. "
                "Install with: pip install psycopg2-binary"
            )

        self._pg_pool = psycopg2.pool.SimpleConnectionPool(
            1, 5, connection_string,
            connect_timeout=10,
            options="-c timezone=UTC"
        )
        self._conn = self._pg_pool.getconn()
        self._conn.autocommit = False
        logger.info("PostgreSQL connection established: %s", urlparse(connection_string).hostname)

    def _migrate(self) -> None:
        with self._lock:
            schema_sql = SCHEMA_PATH.read_text()

            if self._is_postgres:
                # Convert SQLite syntax to PostgreSQL
                schema_sql = self._convert_schema_to_postgres(schema_sql)
                cursor = self._conn.cursor()
                cursor.execute(schema_sql)
                cursor.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (SCHEMA_VERSION, "1"),
                )
                self._conn.commit()
                cursor.close()
            else:
                self._conn.executescript(schema_sql)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    (SCHEMA_VERSION, "1"),
                )
                self._conn.commit()

    def _convert_schema_to_postgres(self, schema_sql: str) -> str:
        """Convert SQLite schema to PostgreSQL syntax."""
        # Replace SQLite-specific syntax
        schema = schema_sql.replace("AUTOINCREMENT", "")
        schema = schema.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
        schema = schema.replace("PRAGMA", "-- PRAGMA")
        # Replace SQLite's UNIQUE constraint syntax for PostgreSQL
        schema = schema.replace("UNIQUE (", "UNIQUE (")
        return schema

    def _convert_params(self, sql: str, params: tuple) -> tuple[str, tuple]:
        """Convert SQLite SQL and params to PostgreSQL format if needed."""
        if not self._is_postgres:
            return sql, params
        # Convert ? placeholders to %s for PostgreSQL
        sql_pg = sql.replace("?", "%s")
        return sql_pg, params

    # --- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement; returns lastrowid."""
        with self._lock:
            params_tuple = tuple(params)
            sql_conv, params_conv = self._convert_params(sql, params_tuple)

            if self._is_postgres:
                cursor = self._conn.cursor()
                cursor.execute(sql_conv, params_conv)
                self._conn.commit()
                try:
                    cursor.execute("SELECT lastval() as id")
                    result = cursor.fetchone()[0] if cursor.fetchone() else 0
                except Exception:
                    result = 0
                cursor.close()
                return result
            else:
                cur = self._conn.execute(sql_conv, params_conv)
                self._conn.commit()
                return cur.lastrowid or 0

    def execute_rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement; returns affected-row count."""
        with self._lock:
            params_tuple = tuple(params)
            sql_conv, params_conv = self._convert_params(sql, params_tuple)

            if self._is_postgres:
                cursor = self._conn.cursor()
                cursor.execute(sql_conv, params_conv)
                self._conn.commit()
                rowcount = cursor.rowcount
                cursor.close()
                return rowcount if rowcount > 0 else 0
            else:
                cur = self._conn.execute(sql_conv, params_conv)
                self._conn.commit()
                return cur.rowcount if cur.rowcount > 0 else 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            params_tuple = tuple(params)
            sql_conv, params_conv = self._convert_params(sql, params_tuple)

            if self._is_postgres:
                cursor = self._conn.cursor()
                cursor.execute(sql_conv, params_conv)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                cursor.close()
                return [dict(zip(columns, row)) for row in rows]
            else:
                cur = self._conn.execute(sql_conv, params_conv)
                return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        """Execute several writes atomically."""
        with self._lock:
            try:
                if self._is_postgres:
                    cursor = self._conn.cursor()
                    for sql, params in statements:
                        sql_conv, params_conv = self._convert_params(sql, tuple(params))
                        cursor.execute(sql_conv, params_conv)
                    self._conn.commit()
                    cursor.close()
                else:
                    for sql, params in statements:
                        sql_conv, _ = self._convert_params(sql, tuple(params))
                        self._conn.execute(sql_conv, tuple(params))
                    self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- kv state --------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        if self._is_postgres:
            # PostgreSQL UPSERT syntax
            sql = (
                "INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
            )
            with self._lock:
                cursor = self._conn.cursor()
                cursor.execute(sql, (key, value, time.time()))
                self._conn.commit()
                cursor.close()
        else:
            # SQLite UPSERT syntax
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
            if self._is_postgres:
                self._pg_pool.putconn(self._conn)
                self._pg_pool.closeall()
            else:
                self._conn.close()
