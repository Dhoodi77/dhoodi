"""Executive agent: final coordinator.

Builds the structured decision object from all agent verdicts and applies
deterministic combination rules:

  - Risk agent reject  -> vetoed, final.
  - Critic reject      -> rejected (thesis failed adversarial review).
  - needs_evidence     -> counted; adaptive confirmations must still be met.
  - Confirmations      -> number of independent buy/approve verdicts must
                          reach the risk agent's adaptive requirement.

An optional LLM pass writes the executive rationale, but it cannot flip a
veto or manufacture confirmations. The output is a decision object, not a
trade: deterministic pipeline code converts approvals into TradeInstruction
and the RiskEngine re-checks everything at execution time.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from tradeos.agents.base import AgentVerdict, BaseAgent


@dataclass
class DecisionObject:
    opportunity_id: str
    final: str                          # execute | reject | investigate
    rationale: str
    verdicts: list[AgentVerdict] = field(default_factory=list)
    confirmations: int = 0
    required_confirmations: int = 1
    disagreements: list[str] = field(default_factory=list)


class ExecutiveAgent(BaseAgent):
    name = "executive"
    role = "decision"

    async def decide(self, opportunity_id: str,
                     verdicts: list[AgentVerdict]) -> DecisionObject:
        self.heartbeat(task=f"decide {opportunity_id}")
        by_agent = {v.agent: v for v in verdicts}
        risk = by_agent.get("risk")
        critic = by_agent.get("critic")

        required = 1
        if risk is not None:
            required = int(risk.evidence.get("required_confirmations", 1))

        confirmations = sum(
            1 for v in verdicts
            if v.agent not in ("risk", "critic") and v.verdict in ("buy", "approve")
        )
        if critic is not None and critic.verdict == "approve":
            confirmations += 1

        disagreements = []
        stances = {v.agent: v.verdict for v in verdicts}
        if len({s for s in stances.values() if s in ("buy", "reject")}) > 1:
            disagreements = [f"{a}:{s}" for a, s in stances.items()]

        if risk is not None and risk.verdict == "reject":
            final, rationale = "reject", f"risk agent veto: {risk.reasoning}"
        elif critic is not None and critic.verdict == "reject":
            final, rationale = "reject", f"failed critic review: {critic.reasoning}"
        elif critic is not None and critic.verdict == "needs_evidence":
            final, rationale = "investigate", "critic requires additional evidence"
        elif confirmations >= required:
            final = "execute"
            rationale = (f"{confirmations}/{required} confirmations met; "
                         "risk approved; thesis survived criticism")
        else:
            final = "reject"
            rationale = f"only {confirmations}/{required} confirmations"

        if self.llm.available and final == "execute":
            summary = await self.llm.complete(
                self.role,
                "You are the executive agent. Write a 2-3 sentence auditable rationale "
                "for the final decision, citing the agent verdicts. You cannot change "
                "the decision.",
                json.dumps({"decision": final, "verdicts": [
                    {"agent": v.agent, "verdict": v.verdict,
                     "confidence": v.confidence, "reasoning": v.reasoning[:200]}
                    for v in verdicts]}),
                max_tokens=512,
            )
            if summary:
                rationale = f"{rationale} | {summary.strip()[:600]}"

        decision = DecisionObject(opportunity_id, final, rationale, verdicts,
                                  confirmations, required, disagreements)
        self.record(opportunity_id, AgentVerdict(
            self.name,
            "buy" if final == "execute" else ("needs_evidence" if final == "investigate" else "reject"),
            0.8, rationale,
            {"confirmations": confirmations, "required": required,
             "disagreements": disagreements},
            self.llm.models[self.role] if self.llm.available else "heuristic",
        ))
        self.db.audit("executive", f"decision_{final}", opportunity_id, {
            "rationale": rationale,
            "verdicts": {v.agent: v.verdict for v in verdicts},
        })
        return decision
