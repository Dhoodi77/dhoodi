"""Risk agent.

Independent safety layer with veto authority. Its analytical verdict wraps
the deterministic red-flag scan plus an adaptive confirmation model: the
riskier the token (age, liquidity, concentration, volatility), the more
independent confirmation it demands before approving. Note this agent is
advisory *on top of* the RiskEngine — even an approve here still passes
through the deterministic engine at execution time.
"""
from __future__ import annotations

from tradeos.agents.base import AgentVerdict, BaseAgent
from tradeos.providers.base import PairData
from tradeos.strategies.scoring import OpportunityScore


class RiskAgent(BaseAgent):
    name = "risk"
    role = "fast"

    def required_confirmations(self, pair: PairData, score: OpportunityScore) -> int:
        """Adaptive confirmation: number of independent BUY/approve verdicts
        (beyond this agent) needed. Ranges 1..4."""
        required = 1
        if pair.age_hours is not None and pair.age_hours < 24:
            required += 1
        if pair.liquidity_usd < self.settings.min_liquidity_usd * 2:
            required += 1
        if score.risk >= 50 or abs(pair.price_change_24h) > 200:
            required += 1
        return min(required, 4)

    async def analyze(self, opportunity_id: str, pair: PairData,
                      score: OpportunityScore) -> AgentVerdict:
        self.heartbeat(task=f"risk {pair.symbol}")
        evidence = {
            "risk_score": score.risk,
            "red_flags": score.red_flags,
            "required_confirmations": self.required_confirmations(pair, score),
        }
        if score.red_flags:
            verdict = AgentVerdict(
                self.name, "reject", 0.9,
                "red flags present: " + "; ".join(score.red_flags), evidence)
        elif score.risk >= 70:
            verdict = AgentVerdict(
                self.name, "reject", 0.8,
                f"risk score {score.risk} too high", evidence)
        elif score.risk >= 45:
            verdict = AgentVerdict(
                self.name, "needs_evidence", 0.6,
                f"moderate risk {score.risk}: requires "
                f"{evidence['required_confirmations']} confirmations", evidence)
        else:
            verdict = AgentVerdict(
                self.name, "approve", 0.7,
                f"risk score {score.risk} acceptable; "
                f"{evidence['required_confirmations']} confirmations required", evidence)
        self.record(opportunity_id, verdict)
        return verdict
