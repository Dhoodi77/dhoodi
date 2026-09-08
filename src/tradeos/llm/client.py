"""LLM client with role-based model routing.

Roles (fast / reasoning / decision) map to configurable Anthropic model ids
so cheap models handle classification and strong models handle strategy. If
no ANTHROPIC_API_KEY (or other SDK credential) is available, `available` is
False and callers must fall back to deterministic heuristics — the pipeline
never blocks on the LLM, and LLM output is never the security boundary.

Secrets: the SDK reads credentials from the environment itself; this module
never touches, stores, or logs key material.
"""
from __future__ import annotations

import json
import logging
import os
import re

import anthropic

from tradeos.config import Settings

logger = logging.getLogger(__name__)


class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.models = {
            "fast": settings.model_fast,
            "reasoning": settings.model_reasoning,
            "decision": settings.model_decision,
        }
        self.available = bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        self._client = anthropic.AsyncAnthropic() if self.available else None

    async def complete(self, role: str, system: str, user: str,
                       max_tokens: int = 2048) -> str | None:
        """One completion for the given role's model. None on any failure —
        callers degrade to heuristics rather than crash the pipeline."""
        if not self._client:
            return None
        model = self.models.get(role, self.settings.model_reasoning)
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            if response.stop_reason == "refusal":
                logger.warning("llm refusal for role=%s", role)
                return None
            return "".join(b.text for b in response.content if b.type == "text")
        except anthropic.APIError as exc:
            logger.warning("llm call failed role=%s model=%s: %s", role, model, exc)
            return None

    async def complete_json(self, role: str, system: str, user: str,
                            max_tokens: int = 2048) -> dict | None:
        """Completion expected to be a single JSON object; tolerant parser."""
        text = await self.complete(role, system, user, max_tokens)
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            logger.warning("llm returned unparseable json for role=%s", role)
            return None
