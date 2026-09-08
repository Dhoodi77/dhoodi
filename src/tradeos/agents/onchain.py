"""On-chain agent.

Uses real chain providers when RPC endpoints are configured; otherwise
reports needs_evidence with reduced confidence instead of inventing chain
data. Holder concentration feeds directly into the risk agent's adaptive
confirmation model.
"""
from __future__ import annotations

from tradeos.agents.base import AgentVerdict, BaseAgent
from tradeos.providers.base import ChainProvider, PairData


class OnChainAgent(BaseAgent):
    name = "onchain"
    role = "fast"

    def __init__(self, settings, db, llm, chain_providers: dict[str, ChainProvider]):
        super().__init__(settings, db, llm)
        self.chain_providers = chain_providers

    async def analyze(self, opportunity_id: str, pair: PairData) -> AgentVerdict:
        self.heartbeat(task=f"onchain {pair.symbol}")
        provider = self.chain_providers.get(pair.chain)
        evidence: dict = {"chain": pair.chain}

        if provider is None:
            verdict = AgentVerdict(self.name, "needs_evidence", 0.3,
                                   f"no provider for chain {pair.chain}", evidence)
            self.record(opportunity_id, verdict)
            return verdict

        holders = None
        try:
            if await provider.is_available():
                holders = await provider.get_holder_summary(pair.token_address)
        except Exception as exc:  # provider failure must not kill the pipeline
            evidence["provider_error"] = str(exc)

        if holders is None:
            verdict = AgentVerdict(
                self.name, "needs_evidence", 0.3,
                f"no on-chain data for {pair.chain}: RPC not configured or unavailable; "
                "holder concentration and contract permissions unverified",
                evidence,
            )
            self.record(opportunity_id, verdict)
            return verdict

        evidence["holders"] = holders
        top10 = holders.get("top10_holder_pct")
        reasons: list[str] = []
        verdict_str, confidence = "approve", 0.6
        if top10 is not None:
            evidence["top10_holder_pct"] = top10
            if top10 > 60:
                verdict_str, confidence = "reject", 0.85
                reasons.append(f"top-10 holders control {top10:.0f}% of supply")
            elif top10 > 40:
                verdict_str, confidence = "needs_evidence", 0.5
                reasons.append(f"elevated concentration: top-10 hold {top10:.0f}%")
            else:
                reasons.append(f"holder concentration acceptable ({top10:.0f}%)")
        else:
            verdict_str, confidence = "needs_evidence", 0.4
            reasons.append("holder distribution unavailable from this provider")

        verdict = AgentVerdict(self.name, verdict_str, confidence,
                               "; ".join(reasons), evidence)
        self.record(opportunity_id, verdict)
        return verdict
