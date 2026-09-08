"""Coding agent — proposal-only by design.

This agent may draft code changes as proposals stored in the database; it
has NO write access to the running codebase and cannot deploy. A proposal
becomes live only through the normal human workflow: review, tests, deploy.
This is a deliberate security boundary, not a missing feature — an agent
that can rewrite its own trading logic can rewrite its own risk limits.
"""
from __future__ import annotations

import time

from tradeos.agents.base import AgentVerdict, BaseAgent


class CodingAgent(BaseAgent):
    name = "coding"
    role = "reasoning"

    async def propose_change(self, title: str, context: str) -> int | None:
        self.heartbeat(task=f"draft proposal: {title[:40]}")
        if not self.llm.available:
            return None
        draft = await self.llm.complete(
            self.role,
            "You are the coding agent of a trading system. Draft a code change "
            "proposal (description + diff sketch). It will be reviewed by a human "
            "before any deployment. Never include secrets.",
            context, max_tokens=4096,
        )
        if not draft:
            return None
        row_id = self.db.execute(
            "INSERT INTO memories (layer, key, content, created_at) VALUES "
            "('agent', ?, ?, ?) ON CONFLICT(layer, key) DO UPDATE SET "
            "content = excluded.content, created_at = excluded.created_at",
            (f"code_proposal_{int(time.time())}", draft, time.time()),
        )
        self.record(None, AgentVerdict(self.name, "hold", 0.5,
                                       f"proposal drafted: {title}", {"proposal_id": row_id},
                                       self.llm.models[self.role]))
        return row_id
