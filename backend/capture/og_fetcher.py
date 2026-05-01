"""Open Graph / Twitter Card / HTML metadata fetcher.

Phase 2.5 Fix 2.A — when a capture arrives with empty `clean_text` and
no transcript, the only thing the bot/extension knows is the URL.
Most URLs that get shared have proper OG / Twitter Card metadata
(news sites, Reddit, IG public posts, FB share links, Substack,
Medium, blogs all do — that's the whole point of OG: be sharable).
We fetch the page once and parse those tags so enrichment has
something to chew on.

Per docs/phase2.5-capture-hydration.md:
  - One HTTP GET per URL miss.
  - 5-second timeout, 2 redirects max, browser User-Agent.
  - Parse with selectolax (fast HTML parser).
  - Tag-priority order: og:* → twitter:* → <title> / <meta name="description">
  - Best-effort: any failure (timeout, non-2xx, malformed HTML) returns None
    and the caller falls through to the next hydration tier.

This module is intentionally pure and side-effect free apart from the
single HTTP GET — it never writes to disk, never logs above DEBUG, and
never raises. The hydration orchestrator (`hydration.py`) decides what
to do with what comes back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

# selectolax is the Phase 2.5 new dep — fast HTML parser, no lxml needed.
# Imported lazily so a missing install doesn't break the import graph
# during partial deploys.
try:
    from selectolax.parser import HTMLParser  # type: ignore
    _SELECTOLAX_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised when dep is missing
    HTMLParser = None  # type: ignore[assignment,misc]
    _SELECTOLAX_AVAILABLE = False


logger = logging.getLogger(__name__)


# Browser-ish User-Agent. Lots of sites (FB share links especially)
# return short/empty bodies to obvious-bot UAs. This isn't an attempt to
# evade — it just gets us the same OG payload Telegram/Slack would see.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_REDIRECTS = 2

# Limit how much HTML we parse. OG tags live in <head>, and 256 KB is
# plenty to find them on every well-behaved site. Saves us from streaming
# down 50-MB pathological pages.
_MAX_HTML_BYTES = 256 * 1024


@dataclass(frozen=True)
class OGMetadata:
    """What we managed to scrape. All fields optional; caller decides
    whether the result is "good enough" to use as hydration."""

    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    site_name: Optional[str] = None
    # Provenance tag — which set of meta tags actually produced the
    # `description`. Helps debugging when a page has both og: and
    # twitter: tags that disagree.
    source: str = "unknown"  # "og" | "twitter" | "html" | "unknown"

    def is_useful(self) -> bool:
        """We consider the fetch worth using if we got a non-empty
        description. Title alone isn't enough — enrichment can do nothing
        with just a title (it would be the same as the existing
        `EmptyContentError` path)."""
        return bool(self.description and self.description.strip())


# ---- Tag extraction --------------------------------------------------

_OG_FIELDS = ("title", "description", "image", "site_name")
_TWITTER_FIELDS = ("title", "description", "image")


def _meta_lookup(tree: "HTMLParser") -> dict[str, str]:
    """Build a flat {key: content} map of all <meta> tags we care about.

    Keys are normalized: `og:title`, `twitter:description`, `description`
    (the plain-HTML one). Values are the raw `content` attribute, stripped.
    First occurrence wins — good enough for OG, which is supposed to be
    unique per page anyway.
    """
    out: dict[str, str] = {}
    for node in tree.css("meta"):
        attrs = node.attributes or {}
        # OG uses `property`, Twitter and plain HTML use `name`.
        key = attrs.get("property") or attrs.get("name")
        if not key:
            continue
        key = key.strip().lower()
        if key in out:
            continue
        content = (attrs.get("content") or "").strip()
        if not content:
            continue
        out[key] = content
    return out


def _parse_metadata(html: str) -> OGMetadata:
    """Pure HTML→OGMetadata. No I/O. Returns an empty OGMetadata on any
    parse error so callers don't need a try-block."""
    if not _SELECTOLAX_AVAILABLE:
        logger.warning(
            "selectolax not installed; OG metadata fetch disabled. "
            "pip install selectolax."
        )
        return OGMetadata()

    try:
        tree = HTMLParser(html)
    except Exception as e:  # noqa: BLE001
        logger.debug("HTML parse failed: %s", e)
        return OGMetadata()

    meta = _meta_lookup(tree)

    # Tier A — Open Graph
    og_title = meta.get("og:title")
    og_desc = meta.get("og:description")
    og_image = meta.get("og:image")
    og_site = meta.get("og:site_name")

    # Tier B — Twitter Card
    tw_title = meta.get("twitter:title")
    tw_desc = meta.get("twitter:description")
    tw_image = meta.get("twitter:image") or meta.get("twitter:image:src")

    # Tier C — plain HTML
    html_title_node = tree.css_first("title")
    html_title = html_title_node.text(strip=True) if html_title_node else None
    html_desc = meta.get("description")

    # Pick the description first (it's what `is_useful` keys on) so we
    # know which `source` tag to set.
    if og_desc:
        description, source = og_desc, "og"
    elif tw_desc:
        description, source = tw_desc, "twitter"
    elif html_desc:
        description, source = html_desc, "html"
    else:
        description, source = None, "unknown"

    title = og_title or tw_title or html_title
    image = og_image or tw_image
    site = og_site

    return OGMetadata(
        title=(title or None) and title.strip(),
        description=(description or None) and description.strip(),
        image_url=(image or None) and image.strip(),
        site_name=(site or None) and site.strip(),
        source=source,
    )


# ---- Network -----------------------------------------------------------

async def fetch_og_metadata(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> OGMetadata | None:
    """Fetch URL and extract OG / Twitter / HTML metadata.

    Returns None on any network error, non-2xx response, or parse failure
    — caller falls through to the next hydration tier. Returns an
    `OGMetadata` (possibly with `is_useful()` False) if the request
    succeeded but the page had no usable tags.

    The optional `client` lets callers inject a shared httpx client (e.g.
    for connection pooling across many captures) and lets tests inject a
    `MockTransport` without touching the network.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    if not _SELECTOLAX_AVAILABLE:
        # No parser → nothing useful we can return. Log once at info,
        # not warning: missing dep is a deployment issue, not a runtime
        # bug, and we don't want to spam the worker log.
        logger.info("OG fetch skipped — selectolax not installed")
        return None

    headers = {
        "User-Agent": user_agent,
        # Be polite about what we want — some CDNs vary their response
        # by Accept header.
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en;q=0.9",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            max_redirects=max_redirects,
            headers=headers,
        )
    assert client is not None  # for type-checkers

    try:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.debug("OG fetch network error for %s: %s", url, e)
            return None

        if resp.status_code >= 400:
            logger.debug("OG fetch %s → HTTP %d", url, resp.status_code)
            return None

        # Some sites stream multi-MB pages even though the OG tags are in
        # the first KB. Slice the body to keep parser memory bounded.
        body = resp.content[:_MAX_HTML_BYTES]
        try:
            html = body.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = body.decode("utf-8", errors="replace")

        meta = _parse_metadata(html)
        logger.debug(
            "OG fetch %s → source=%s title=%r desc_len=%d",
            url, meta.source, (meta.title or "")[:60],
            len(meta.description or ""),
        )
        return meta
    finally:
        if own_client:
            await client.aclose()
