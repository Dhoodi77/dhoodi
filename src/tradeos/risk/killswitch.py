"""Emergency kill switch.

Implemented entirely outside the AI reasoning layer: a database flag plus a
filesystem sentinel. Either one being set halts all execution. The file
sentinel exists so the operator can kill trading even if the API/DB is
misbehaving: `touch KILLSWITCH` next to the database file.

Resume requires passing deterministic safety checks (see RiskEngine.resume).
"""
from __future__ import annotations

import time
from pathlib import Path

from tradeos.db.database import Database

KV_KEY = "kill_switch"
SENTINEL_NAME = "KILLSWITCH"


class KillSwitch:
    def __init__(self, db: Database):
        self.db = db
        db_path = Path(db.path) if db.path != ":memory:" else None
        self.sentinel = (db_path.parent / SENTINEL_NAME) if db_path else None

    def is_active(self) -> bool:
        if self.db.kv_get(KV_KEY) == "1":
            return True
        if self.sentinel is not None and self.sentinel.exists():
            return True
        return False

    def activate(self, actor: str, reason: str) -> None:
        self.db.kv_set(KV_KEY, "1")
        self.db.kv_set(f"{KV_KEY}_reason", reason)
        self.db.execute(
            "INSERT INTO risk_events (kind, rule, detail, created_at) VALUES (?, ?, ?, ?)",
            ("kill_switch", "activate", f"{actor}: {reason}", time.time()),
        )
        self.db.audit(actor, "kill_switch_activate", detail={"reason": reason})
        self.db.alert("critical", "KILL SWITCH ACTIVATED", reason)

    def deactivate(self, actor: str) -> None:
        if self.sentinel is not None and self.sentinel.exists():
            # File sentinel must be removed manually by the operator; refusing
            # keeps a shell-level kill authoritative over the API.
            raise RuntimeError(
                f"kill switch sentinel file still present: {self.sentinel}"
            )
        self.db.kv_set(KV_KEY, "0")
        self.db.audit(actor, "kill_switch_deactivate")
        self.db.alert("high", "Kill switch deactivated", f"by {actor}")

    def reason(self) -> str | None:
        return self.db.kv_get(f"{KV_KEY}_reason")
