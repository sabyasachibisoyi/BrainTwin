"""Enrichment prompts.

Kept separate from `enrichment.py` so the prompt can be iterated on
without touching call code or tests. The system prompt encodes
docs/phase2-design.md Decision D (multi-language transliteration rules)
and the schema from Decision A.
"""

from __future__ import annotations

from textwrap import dedent

from backend.config import settings


# The single worked example anchors Haiku's output format. This is the
# highest-leverage thing in the prompt — without it, format drift on
# small models is real.
EXAMPLE_INPUT = """\
Bengaluru's HSR Layout has become unaffordable for young renters, a viral
post on X this week claims. The author, a 28-year-old marketing professional,
said her budget was ₹15,000 for a 1BHK but every listing she found in HSR
started at ₹25,000. Hindustan Times picked up the post on Friday. Real
estate agents quoted in the article say HSR rents have climbed roughly 40%
in the past two years, driven by tech-sector demand and a shortage of
new construction in the area.
"""

EXAMPLE_OUTPUT = """\
{
  "summary": "A viral X post by a 28-year-old in Bengaluru complains that 1BHK rents in HSR Layout start at ₹25,000 vs her ₹15,000 budget; HT covered the post and quoted agents saying HSR rents are up ~40% in two years.",
  "entities": [
    {"name": "Bengaluru", "type": "place"},
    {"name": "HSR Layout", "type": "place"},
    {"name": "Hindustan Times", "type": "org"},
    {"name": "X", "type": "org"}
  ],
  "key_facts": [
    "1BHK rents in HSR Layout start at ~₹25,000",
    "Renter's stated budget was ₹15,000",
    "HSR rents up ~40% over the past two years",
    "Author of the viral post is a 28-year-old marketing professional"
  ],
  "topics": ["bengaluru", "rent-crisis", "indian-cities", "real-estate", "viral-post"]
}
"""


def build_system_prompt() -> str:
    """The system prompt — uses configured user_languages so the prompt
    can be repointed at someone else's language profile via .env later
    (see Decision D portability note).
    """
    return dedent(
        f"""\
        You are an information extractor for a personal knowledge twin.

        Source content can be in any of these languages, often code-switched:
        {settings.user_languages}.
        You read all of them fluently. The source content you're given may
        be in any of them or a mix.

        Your job: read the content and return a JSON object describing it,
        following the schema and language rules below exactly.

        SCHEMA — return EXACTLY these four fields, no others:
          - summary:    string. 1–2 sentences. Plain English.
          - entities:   array of {{"name": "...", "type": "person|org|place|event"}}.
                        Names transliterated to Latin script (NOT translated).
                        दीपिका पादुकोण → "Deepika Padukone".
                        ఎన్టీఆర్ → "NTR".
                        Already-Latin names stay verbatim ("Schadenfreude").
          - key_facts:  array of strings. Specific, atomic claims with
                        numbers/dates/names where relevant. The quiz mode
                        will use these as the answer source — make them
                        crisp and standalone.
          - topics:     array of 3–5 lowercase, hyphenated free-text tags
                        (English). Examples: "bengaluru", "rent-crisis",
                        "bollywood-controversy", "test-cricket".

        LANGUAGE RULES (Decision D):
          - All output strings: English with Latin script.
          - Entity names: transliterate non-Latin scripts (do NOT translate).
          - Cultural keywords, idioms, proper nouns, untranslatable phrases:
            preserve verbatim in their Romanized form within summaries and
            key_facts. Examples: "jugaad", "yaar", "kal mein soya tha",
            "Schadenfreude", movie titles like "Tamasha".
          - Topics: always English, lowercase, hyphenated.

        OUTPUT FORMAT:
          - Return ONLY the JSON object. No prose, no preamble, no markdown
            fences. Start with {{ and end with }}.
          - If a field has no values, return an empty array — never null.
          - If the content is empty or pure noise, still return all four
            fields with sensible empty values; do not refuse.

        EXAMPLE
        Input:
        {EXAMPLE_INPUT.strip()}

        Output:
        {EXAMPLE_OUTPUT.strip()}
        """
    ).strip()


def build_user_prompt(
    *,
    title: str,
    url: str,
    platform: str,
    text: str,
) -> str:
    """The user message — wraps the captured content with a tiny header
    of source metadata so Haiku has context for what it's looking at."""
    return dedent(
        f"""\
        SOURCE
        title:    {title}
        url:      {url}
        platform: {platform}

        CONTENT
        {text}
        """
    ).strip()


# Used on the single retry after MalformedResponseError. Tightens the
# format instruction without rewriting the whole prompt.
RETRY_REMINDER = dedent(
    """\
    REMINDER: Your previous response was not valid JSON. Return ONLY a
    single JSON object with the four fields summary / entities /
    key_facts / topics. Start with {{ and end with }}. No markdown
    fences, no prose, no preamble.
    """
).strip()
