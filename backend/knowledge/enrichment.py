"""Enrichment — turn ProcessedContent into the 4-field knowledge block.

Phase 2 core. Per docs/phase2-design.md:
  Decision A — schema is summary / entities / key_facts / topics.
  Decision G — send full content up to ~50k tokens (no chunking in v1).
  Decision D — language rules live in the prompt.

This module is the pure "given clean text, produce enrichment dict"
layer. Retry policy and persistence live in `enrichment_worker.py` so
this stays unit-testable without async machinery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.capture.processor import ProcessedContent
from backend.config import settings
from backend.knowledge.llm_client import (
    LLMClient,
    MalformedResponseError,
)
from backend.knowledge.prompts import (
    RETRY_REMINDER,
    build_system_prompt,
    build_user_prompt,
)


logger = logging.getLogger(__name__)


# ---- Errors ----------------------------------------------------------

class EnrichmentError(Exception):
    """Base for enrichment-domain errors (vs LLM-transport errors from llm_client)."""


class EmptyContentError(EnrichmentError):
    """No clean_text, no transcript, no image text — nothing to enrich.
    Logged with reason 'empty_content', skipped without an API call."""


class ContentTooLongError(EnrichmentError):
    """Combined content exceeds enrichment_max_input_chars. Rare — only
    multi-hour transcripts and books. Phase-3+ adds chunking; for now we
    log and skip."""


class SchemaError(EnrichmentError):
    """Model returned valid JSON but missing required fields or wrong types."""


# ---- Schema validation ----------------------------------------------

REQUIRED_KEYS = {"summary", "entities", "key_facts", "topics"}
ALLOWED_ENTITY_TYPES = {"person", "org", "place", "event"}


def _validate(parsed: dict[str, Any]) -> dict[str, Any]:
    """Verify shape and lightly normalize. Raises SchemaError on bad input."""
    missing = REQUIRED_KEYS - parsed.keys()
    if missing:
        raise SchemaError(f"missing required fields: {sorted(missing)}")

    # summary
    summary = parsed.get("summary")
    if not isinstance(summary, str):
        raise SchemaError(f"summary must be string, got {type(summary).__name__}")

    # entities — tolerate slightly malformed entries (drop bad ones)
    raw_entities = parsed.get("entities") or []
    if not isinstance(raw_entities, list):
        raise SchemaError(f"entities must be array, got {type(raw_entities).__name__}")
    entities: list[dict[str, str]] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        etype = e.get("type")
        if not name or not isinstance(name, str):
            continue
        if etype not in ALLOWED_ENTITY_TYPES:
            etype = "event"  # safe fallback rather than dropping
        entities.append({"name": name.strip(), "type": etype})

    # key_facts
    key_facts = parsed.get("key_facts") or []
    if not isinstance(key_facts, list):
        raise SchemaError(f"key_facts must be array, got {type(key_facts).__name__}")
    key_facts = [str(f).strip() for f in key_facts if f and isinstance(f, (str, int, float))]

    # topics
    topics = parsed.get("topics") or []
    if not isinstance(topics, list):
        raise SchemaError(f"topics must be array, got {type(topics).__name__}")
    topics = [str(t).strip().lower() for t in topics if t and isinstance(t, str)]

    return {
        "summary": summary.strip(),
        "entities": entities,
        "key_facts": key_facts,
        "topics": topics,
    }


# ---- Pure enrichment call -------------------------------------------

async def enrich(
    processed: ProcessedContent,
    *,
    client: LLMClient,
    retry_on_bad_json: bool = True,
) -> dict[str, Any]:
    """Run enrichment on a processed capture. Returns the validated
    enrichment dict (without the surrounding wrapper keys — the worker
    adds `model`, `enriched_at`, `capture_id`).

    Raises:
        EmptyContentError      → nothing to enrich
        ContentTooLongError    → exceeds configured cap
        TransientLLMError      → network / 5xx / rate limit (caller retries)
        PermanentLLMError      → 4xx / auth (caller does NOT retry)
        MalformedResponseError → bad JSON after retry (caller logs failure)
        SchemaError            → JSON parsed but wrong shape (caller logs failure)
    """
    text = processed.combined_text.strip()
    if not text:
        raise EmptyContentError("no clean_text, transcript, or image_text to enrich")

    if len(text) > settings.enrichment_max_input_chars:
        raise ContentTooLongError(
            f"content is {len(text)} chars, max is {settings.enrichment_max_input_chars}"
        )

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        title=processed.title,
        url=processed.url,
        platform=processed.platform,
        text=text,
    )

    try:
        parsed = await client.enrich(
            text=text,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except MalformedResponseError:
        if not retry_on_bad_json:
            raise
        # Single retry with a stricter format reminder (Decision H).
        logger.warning("Enrichment returned malformed JSON; retrying with reminder")
        parsed = await client.enrich(
            text=text,
            system_prompt=system_prompt + "\n\n" + RETRY_REMINDER,
            user_prompt=user_prompt,
        )

    return _validate(parsed)


def wrap_enrichment_record(
    *,
    capture_id: str,
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    """Build the row that goes into data/enrichments.jsonl."""
    return {
        "capture_id": capture_id,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.enrichment_model,
        "enrichment": enrichment,
        # Reserved for Phase 3 cross-language linking — empty in v1
        # per Decision I.
        "related_captures": [],
    }
