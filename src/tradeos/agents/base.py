"""Agent framework.

Agents are reasoning components: they observe data and produce verdicts with
confidence and evidence. They never execute anything themselves — execution
goes through the deterministic ExecutionGateway. Every verdict is persisted
to agent_decisions with the model used ('heuristic' when no LLM ran), so
decisions stay explainable and auditable.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.llm.client import LlmClient

logger = logging.getLogger(__name__)

VERDICTS = ("buy", "sell", "hold", "approve", "reject", "needs_evidence")


@dataclass
class AgentVerdict:
    agent: str
    verdict: str                       # one of VERDICTS
    confidence: float                  # 0..1
    reasoning: str
    evidence: dict = field(default_factory=dict)
    model_used: str = "heuristic"

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            self.verdict = "hold"
        self.confidence = max(0.0, min(1.0, self.confidence))


class BaseAgent:
    name = "base"
    role = "reasoning"  # llm routing role: fast | reasoning | decision

    def __init__(self, settings: Settings, db: Database, llm: LlmClient):
        self.settings = settings
        self.db = db
        self.llm = llm

    def heartbeat(self, status: str = "ok", task: str = "") -> None:
        self.db.kv_set(
            f"agent_hb_{self.name}",
            json.dumps({"ts": time.time(), "status": status, "task": task}),
        )

    def record(self, opportunity_id: str | None, verdict: AgentVerdict) -> None:
        self.db.execute(
            "INSERT INTO agent_decisions (opportunity_id, agent, verdict, confidence, "
            "reasoning, evidence_json, model_used, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (opportunity_id, verdict.agent, verdict.verdict, verdict.confidence,
             verdict.reasoning, json.dumps(verdict.evidence, default=str),
             verdict.model_used, time.time()),
        )

    async def ask_llm_json(self, system: str, user: str) -> dict | None:
        return await self.llm.complete_json(self.role, system, user)
