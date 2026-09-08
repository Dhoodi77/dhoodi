"""Web agent — source-scored external intelligence.

Real search/social integrations (X, Reddit, web search) require API
credentials that are not configured yet; this agent degrades to
needs_evidence rather than fabricating narrative. The source reliability
model is live and used by the research agent's report format.

Required credentials to activate (documented, not invented):
  TRADEOS_SEARCH_API_KEY / TRADEOS_SEARCH_PROVIDER  (e.g. brave, serpapi)
  TRADEOS_X_BEARER_TOKEN                            (X/Twitter API v2)
  TRADEOS_REDDIT_CLIENT_ID / TRADEOS_REDDIT_SECRET  (Reddit OAuth)
"""
from __future__ import annotations

import os

from tradeos.agents.base import AgentVerdict, BaseAgent

# Source reliability priors, 0..1. A random social post never equals
# primary-source data.
SOURCE_RELIABILITY = {
    "official_docs": 0.9,
    "block_explorer": 0.9,
    "dexscreener": 0.8,
    "established_news": 0.6,
    "reddit": 0.35,
    "x_twitter": 0.3,
    "telegram": 0.2,
    "unknown": 0.1,
}


class WebAgent(BaseAgent):
    name = "web"
    role = "fast"

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("TRADEOS_SEARCH_API_KEY"))

    async def analyze(self, opportunity_id: str, query: str) -> AgentVerdict:
        self.heartbeat(task=f"web search: {query[:40]}")
        if not self.configured:
            verdict = AgentVerdict(
                self.name, "needs_evidence", 0.2,
                "no web/search credentials configured; social and news signals "
                "unavailable (set TRADEOS_SEARCH_API_KEY to enable)",
                {"source_reliability_model": SOURCE_RELIABILITY},
            )
            self.record(opportunity_id, verdict)
            return verdict
        # Integration point: provider-specific search implementation goes here
        # when credentials exist. Deliberately unreachable until configured.
        raise NotImplementedError("search provider integration pending credentials")
