"""Critic / verifier agent.

Tries to DISPROVE the trade thesis. Deterministic counter-evidence scan
first (distribution, exhausted move, late entry, thin books, one-sided
flow); an LLM contrarian pass can add objections but can never remove the
deterministic ones. A critic 'reject' forces the executive to stand down or
gather more evidence — agreement is not a substitute for surviving
criticism.
"""
from __future__ import annotations

from tradeos.agents.base import AgentVerdict, BaseAgent
from tradeos.providers.base import PairData
from tradeos.strategies.scoring import OpportunityScore


class CriticAgent(BaseAgent):
    name = "critic"
    role = "decision"

    def counter_evidence(self, pair: PairData, score: OpportunityScore) -> list[str]:
        objections: list[str] = []
        if pair.price_change_24h > 150 and pair.price_change_1h < 0:
            objections.append("entry looks late: big 24h move already fading in the last hour")
        if pair.price_change_5m < -3:
            objections.append(f"price dropping now ({pair.price_change_5m:.1f}% in 5m)")
        sell_total = pair.sells_1h + pair.sells_5m
        buy_total = pair.buys_1h + pair.buys_5m
        if sell_total > buy_total * 1.5 and sell_total > 10:
            objections.append("sell transactions dominate: likely distribution, not accumulation")
        if pair.liquidity_usd > 0 and pair.volume_1h > pair.liquidity_usd * 2:
            objections.append("1h volume more than 2x liquidity: volume may be manipulated")
        if score.risk >= 40:
            objections.append(f"aggregate risk score {score.risk} is material")
        if pair.volume_5m == 0 and pair.volume_1h > 0:
            objections.append("momentum stalled: zero volume in the last 5 minutes")
        return objections

    async def analyze(self, opportunity_id: str, pair: PairData,
                      score: OpportunityScore, thesis: str) -> AgentVerdict:
        self.heartbeat(task=f"critique {pair.symbol}")
        objections = self.counter_evidence(pair, score)
        model_used = "heuristic"

        if self.llm.available:
            extra = await self.ask_llm_json(
                "You are the contrarian critic of a crypto trading system. Your job is "
                "to DISPROVE the trade thesis. List concrete failure modes supported by "
                'the data. Respond with JSON: {"objections": [str], '
                '"fatal": bool, "reasoning": str}. Only mark fatal if the data itself '
                "contradicts the thesis.",
                f"Thesis: {thesis}\nMarket data: price={pair.price_usd}, "
                f"liq=${pair.liquidity_usd:,.0f}, vol24h=${pair.volume_24h:,.0f}, "
                f"chg 5m/1h/6h/24h = {pair.price_change_5m}/{pair.price_change_1h}/"
                f"{pair.price_change_6h}/{pair.price_change_24h}%, "
                f"tx 1h buys/sells={pair.buys_1h}/{pair.sells_1h}, "
                f"age={pair.age_hours}h, risk_score={score.risk}, "
                f"red_flags={score.red_flags}",
            )
            if extra is not None:
                model_used = self.llm.models[self.role]
                objections += [str(o) for o in extra.get("objections", [])][:8]
                if extra.get("fatal"):
                    objections.append("critic-llm: fatal objection — "
                                      + str(extra.get("reasoning", ""))[:300])

        fatal = any("fatal" in o for o in objections)
        strong = len(objections)
        if fatal or strong >= 3:
            verdict = AgentVerdict(self.name, "reject", min(0.95, 0.5 + strong * 0.1),
                                   "thesis does not survive criticism",
                                   {"objections": objections}, model_used)
        elif strong == 2:
            verdict = AgentVerdict(self.name, "needs_evidence", 0.6,
                                   "material objections require more confirmation",
                                   {"objections": objections}, model_used)
        else:
            verdict = AgentVerdict(self.name, "approve", 0.65,
                                   "thesis survived contrarian review",
                                   {"objections": objections}, model_used)
        self.record(opportunity_id, verdict)
        return verdict
