"""Model-agnostic LLM client.

Wraps the `anthropic` async SDK behind an interface (`enrich`, `answer`)
that can be swapped to a local model later (Phase 5+ Llama A/B test, see
docs/phase2-design.md Decision F). Knowing nothing about the wrapping
keeps the rest of the codebase model-portable.

Errors are typed so the caller can decide retry policy:

    TransientLLMError      → retry with backoff (network blip, 5xx, rate limit)
    MalformedResponseError → retry once with stricter prompt (bad JSON)
    PermanentLLMError      → don't retry (4xx auth, content too long)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from backend.config import settings


logger = logging.getLogger(__name__)


# ---- Errors ----------------------------------------------------------

class LLMError(Exception):
    """Base for everything this module raises."""


class TransientLLMError(LLMError):
    """Retryable — network, 5xx, rate limit."""


class PermanentLLMError(LLMError):
    """Don't retry — 4xx auth, request invalid, content too long for context."""


class MalformedResponseError(LLMError):
    """Model returned text we couldn't parse as the expected JSON shape."""


# ---- Client ---------------------------------------------------------

class LLMClient:
    """Thin async wrapper over `anthropic.AsyncAnthropic`.

    Stateless aside from the underlying HTTP client. Construct once at
    process startup, share across requests. Caller does retry + JSONL
    persistence — this module just talks to Claude and parses responses.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        key = api_key if api_key is not None else settings.anthropic_api_key
        if not key:
            raise PermanentLLMError(
                "ANTHROPIC_API_KEY is empty — set it in .env. See .env.example."
            )
        self._client = anthropic.AsyncAnthropic(api_key=key)
        self._enrichment_model = model or settings.enrichment_model

    async def aclose(self) -> None:
        await self._client.close()

    # ---- Public API -------------------------------------------------

    async def enrich(
        self,
        *,
        text: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Call Haiku with the enrichment prompt and parse JSON out.

        The prompts are passed in (built by `prompts.py`) so this stays
        model-mechanic only — no business logic here.
        """
        try:
            response = await self._client.messages.create(
                model=self._enrichment_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIConnectionError as e:
            raise TransientLLMError(f"connection: {e}") from e
        except anthropic.RateLimitError as e:
            raise TransientLLMError(f"rate_limit: {e}") from e
        except anthropic.AuthenticationError as e:
            raise PermanentLLMError(f"auth: {e}") from e
        except anthropic.BadRequestError as e:
            # 400 — usually content-too-long or malformed request. Don't retry.
            raise PermanentLLMError(f"bad_request: {e}") from e
        except anthropic.APIStatusError as e:
            # 5xx → transient, 4xx → permanent
            status = getattr(e, "status_code", 0) or 0
            if 500 <= status < 600:
                raise TransientLLMError(f"server_{status}: {e}") from e
            raise PermanentLLMError(f"http_{status}: {e}") from e
        except anthropic.APIError as e:
            # Unknown SDK error — treat as transient (better to retry once
            # than silently lose enrichment for a bug we don't recognize).
            raise TransientLLMError(f"api: {e}") from e

        # Extract text from the response (Anthropic returns a list of blocks).
        try:
            raw = "".join(
                block.text for block in response.content if hasattr(block, "text")
            ).strip()
        except Exception as e:  # noqa: BLE001
            raise MalformedResponseError(f"could not read response blocks: {e}") from e

        if not raw:
            raise MalformedResponseError("empty response from model")

        # Strip markdown fences if Haiku wraps the JSON despite instructions.
        raw = _strip_code_fence(raw)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MalformedResponseError(
                f"response is not valid JSON: {e.msg} (got: {raw[:200]!r})"
            ) from e

        if not isinstance(parsed, dict):
            raise MalformedResponseError(
                f"expected JSON object at top level, got {type(parsed).__name__}"
            )

        return parsed

    async def answer(self, *, question: str, snippets: list[dict[str, Any]]) -> str:
        """Phase 4 stub — agent answer with inline citations.

        Not implemented in Phase 2. The interface is here so we don't have
        to refactor `LLMClient` callers when we wire the agent.
        """
        raise NotImplementedError("Phase 4 — agent layer not yet built")


# ---- Helpers --------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """Haiku occasionally wraps JSON in ```json ... ``` despite being told not to.
    Strip leading/trailing fences if present.
    """
    t = text.strip()
    if t.startswith("```"):
        # Drop first line (the fence with optional language tag)
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t[: -3]
    return t.strip()
