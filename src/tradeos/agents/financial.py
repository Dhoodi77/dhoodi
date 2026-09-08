"""Financial agent: market structure, liquidity, momentum, risk/reward.

The core analysis is deterministic (from PairData and the momentum engine);
an LLM pass, when available, can only refine reasoning text and confidence,
never override the numeric gates.
"""
from __future__ import annotations

from tradeos.agents.base import AgentVerdict, BaseAgent
from tradeos.providers.base import PairData
from tradeos.strategies.momentum import analyze_momentum


class FinancialAgent(BaseAgent):
    name = "financial"
    role = "reasoning"

    async def analyze(self, opportunity_id: str, pair: PairData) -> AgentVerdict:
        self.heartbeat(task=f"analyze {pair.symbol}")
        momentum = analyze_momentum(pair)
        s = self.settings

        evidence = {
            "momentum_score": momentum.score,
            "momentum_components": momentum.components,
            "liquidity_usd": pair.liquidity_usd,
            "volume_24h": pair.volume_24h,
            "price_change_1h": pair.price_change_1h,
            "price_change_24h": pair.price_change_24h,
        }

        reasons: list[str] = []
        verdict = "hold"
        confidence = 0.5

        if pair.liquidity_usd < s.min_liquidity_usd:
            verdict, confidence = "reject", 0.9
            reasons.append(f"liquidity ${pair.liquidity_usd:,.0f} below floor")
        elif pair.volume_24h < s.min_volume_24h_usd:
            verdict, confidence = "reject", 0.8
            reasons.append(f"24h volume ${pair.volume_24h:,.0f} below floor")
        elif momentum.score >= 65:
            verdict = "buy"
            confidence = min(0.9, 0.4 + momentum.score / 200)
            reasons.append(f"momentum {momentum.score} with adequate liquidity")
        elif momentum.score <= 35:
            verdict, confidence = "reject", 0.7
            reasons.append(f"weak momentum {momentum.score}")
        else:
            reasons.append(f"momentum {momentum.score} inconclusive")

        # expected risk/reward under configured exits, adjusted by simulated
        # round-trip slippage
        slip = s.paper_simulated_slippage_pct * 2
        reward = s.exit_take_profit_pct - slip
        risk = s.exit_stop_loss_pct + slip
        rr = reward / risk if risk > 0 else 0
        evidence["risk_reward_ratio"] = round(rr, 2)
        if verdict == "buy" and rr < 1.0:
            verdict, confidence = "reject", 0.8
            reasons.append(f"risk/reward {rr:.2f} below 1.0 after slippage")

        model_used = "heuristic"
        if self.llm.available and verdict == "buy":
            refined = await self.ask_llm_json(
                "You are the financial analysis agent of a crypto trading system. "
                "Given deterministic metrics, sanity-check the BUY thesis. Respond "
                'with JSON: {"agree": bool, "confidence": 0..1, "reasoning": str}. '
                "You cannot raise position size or bypass risk limits.",
                f"Metrics: {evidence}\nProposed verdict: buy\nReasons: {reasons}",
            )
            if refined is not None:
                model_used = self.llm.models[self.role]
                if refined.get("agree") is False:
                    verdict = "hold"
                    reasons.append(f"llm disagreed: {refined.get('reasoning', '')[:300]}")
                else:
                    reasons.append(f"llm concurred: {refined.get('reasoning', '')[:300]}")
                try:
                    confidence = min(confidence, float(refined.get("confidence", confidence)))
                except (TypeError, ValueError):
                    pass

        result = AgentVerdict(self.name, verdict, confidence, "; ".join(reasons),
                              evidence, model_used)
        self.record(opportunity_id, result)
        return result
