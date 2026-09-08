"""Planning agent: durable task queue with retry/escalation.

Tasks live in kv-backed rows so unfinished work survives restarts. The
scheduler drains due tasks; failures retry with backoff up to max_attempts,
then escalate as an alert.
"""
from __future__ import annotations

import json
import time

from tradeos.agents.base import BaseAgent


class PlanningAgent(BaseAgent):
    name = "planning"
    role = "fast"

    def _tasks(self) -> list[dict]:
        raw = self.db.kv_get("planning_tasks", "[]")
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []

    def _save(self, tasks: list[dict]) -> None:
        self.db.kv_set("planning_tasks", json.dumps(tasks))

    def enqueue(self, kind: str, payload: dict, priority: int = 5,
                max_attempts: int = 3) -> None:
        tasks = self._tasks()
        tasks.append({
            "id": f"task_{int(time.time() * 1000)}_{len(tasks)}",
            "kind": kind, "payload": payload, "priority": priority,
            "attempts": 0, "max_attempts": max_attempts,
            "not_before": 0.0, "created_at": time.time(),
        })
        self._save(tasks)

    def due_tasks(self) -> list[dict]:
        now = time.time()
        return sorted(
            [t for t in self._tasks() if t["not_before"] <= now],
            key=lambda t: t["priority"],
        )

    def complete(self, task_id: str) -> None:
        self._save([t for t in self._tasks() if t["id"] != task_id])

    def fail(self, task_id: str, error: str) -> None:
        tasks = self._tasks()
        for t in tasks:
            if t["id"] == task_id:
                t["attempts"] += 1
                if t["attempts"] >= t["max_attempts"]:
                    tasks.remove(t)
                    self.db.alert("high", f"task {t['kind']} failed permanently", error)
                    self.db.system_event("task_escalated", f"{t['kind']}: {error}")
                else:
                    t["not_before"] = time.time() + 30 * (2 ** t["attempts"])
                break
        self._save(tasks)
