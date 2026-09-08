"""Research agent.

Separates FACT (observed market data), INFERENCE (derived), and SPECULATION
(unverified) — speculation is never presented as fact. Without web access or
an LLM it reports only what the data supports and lowers confidence
accordingly, instead of inventing narrative.
"""
from __future__ import annotations

import json
import time

from tradeos.agents.base import AgentVerdict, BaseAgent
from tradeos.providers.base import PairData


class ResearchAgent(BaseAgent):
    name = "research"
    role = "reasoning"

    async def analyze(self, opportunity_id: str, pair: PairData) -> AgentVerdict:
        self.heartbeat(task=f"research {pair.symbol}")

        facts = {
            "chain": pair.chain,
            "symbol": pair.symbol,
            "dex": pair.dex,
            "price_usd": pair.price_usd,
            "liquidity_usd": pair.liquidity_usd,
            "volume_24h": pair.volume_24h,
            "age_hours": pair.age_hours,
            "buys_1h": pair.buys_1h,
            "sells_1h": pair.sells_1h,
        }
        inferences: list[str] = []
        speculation: list[str] = []
        sources = [{"name": "dexscreener", "reliability": 0.8,
                    "note": "primary market data source"}]

        if pair.age_hours is not None and pair.age_hours < 24:
            inferences.append("very young token: launch-phase dynamics dominate")
        if pair.liquidity_usd > 0 and pair.volume_24h / max(pair.liquidity_usd, 1) > 5:
            inferences.append("high turnover vs liquidity: strong attention or churn")
        if pair.buys_1h + pair.sells_1h > 0:
            ratio = pair.buys_1h / max(pair.buys_1h + pair.sells_1h, 1)
            inferences.append(f"1h buy ratio {ratio:.2f}")

        verdict, confidence = "hold", 0.4
        reasoning = "market-data-only research; no external narrative sources configured"

        if self.llm.available:
            result = await self.ask_llm_json(
                "You are the research agent of a crypto trading system. You have ONLY "
                "the market data provided — you have no web access here, so anything "
                "not derivable from the data is SPECULATION and must be labeled as "
                'such. Respond with JSON: {"verdict": "buy"|"hold"|"reject", '
                '"confidence": 0..1, "inferences": [str], "speculation": [str], '
                '"reasoning": str}.',
                f"Market facts: {json.dumps(facts, default=str)}",
            )
            if result is not None:
                verdict = result.get("verdict", "hold")
                try:
                    confidence = max(0.0, min(1.0, float(result.get("confidence", 0.4))))
                except (TypeError, ValueError):
                    confidence = 0.4
                inferences += [str(x) for x in result.get("inferences", [])][:10]
                speculation += [str(x) for x in result.get("speculation", [])][:10]
                reasoning = str(result.get("reasoning", ""))[:1000]

        self.db.execute(
            "INSERT INTO research_reports (opportunity_id, agent, fact_json, inference_json, "
            "speculation_json, sources_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (opportunity_id, self.name, json.dumps(facts, default=str),
             json.dumps(inferences), json.dumps(speculation),
             json.dumps(sources), time.time()),
        )
        result_verdict = AgentVerdict(
            self.name, verdict, confidence, reasoning,
            {"facts": facts, "inferences": inferences, "speculation": speculation},
            self.llm.models[self.role] if self.llm.available else "heuristic",
        )
        self.record(opportunity_id, result_verdict)
        return result_verdict
