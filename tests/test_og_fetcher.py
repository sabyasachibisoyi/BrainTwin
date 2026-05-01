"""Tests for backend/capture/og_fetcher.py — Phase 2.5 Fix 2.A.

Run with: pytest tests/test_og_fetcher.py -v

Covers:
  - meta-tag extraction priority (og: > twitter: > html)
  - is_useful() gate (description present vs title-only)
  - URL filter (only http/https accepted)
  - HTTP integration via httpx.MockTransport (no real network)
  - error paths: HTTP 4xx/5xx, network errors, oversized bodies

The selectolax dependency is required for these tests — if it's not
installed they are skipped, mirroring the runtime behaviour of the
fetcher itself.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.capture.og_fetcher import (  # noqa: E402
    OGMetadata,
    _parse_metadata,
    _SELECTOLAX_AVAILABLE,
    fetch_og_metadata,
)


pytestmark = pytest.mark.skipif(
    not _SELECTOLAX_AVAILABLE,
    reason="selectolax not installed; OG fetcher returns None instead of parsing",
)


# ---- _parse_metadata (pure HTML → OGMetadata) -------------------------

class TestParseMetadata:
    def test_full_open_graph(self):
        html = """
        <html><head>
          <meta property="og:title" content="OG Title">
          <meta property="og:description" content="OG description text.">
          <meta property="og:image" content="https://cdn.example.com/i.jpg">
          <meta property="og:site_name" content="Example Site">
          <title>HTML Title</title>
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.title == "OG Title"
        assert meta.description == "OG description text."
        assert meta.image_url == "https://cdn.example.com/i.jpg"
        assert meta.site_name == "Example Site"
        assert meta.source == "og"
        assert meta.is_useful()

    def test_twitter_card_fallback(self):
        # No og: tags — twitter:* should win.
        html = """
        <html><head>
          <meta name="twitter:title" content="TW Title">
          <meta name="twitter:description" content="TW description.">
          <meta name="twitter:image" content="https://cdn.example.com/tw.jpg">
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.source == "twitter"
        assert meta.title == "TW Title"
        assert meta.description == "TW description."
        assert meta.image_url == "https://cdn.example.com/tw.jpg"

    def test_html_fallback_when_no_og_or_twitter(self):
        html = """
        <html><head>
          <title>Plain HTML</title>
          <meta name="description" content="A plain description.">
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.source == "html"
        assert meta.title == "Plain HTML"
        assert meta.description == "A plain description."
        assert meta.image_url is None

    def test_og_wins_over_twitter_and_html(self):
        # All three present: og: should be the canonical pick.
        html = """
        <html><head>
          <meta property="og:description" content="OG wins.">
          <meta name="twitter:description" content="TW loses.">
          <meta name="description" content="HTML loses.">
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.description == "OG wins."
        assert meta.source == "og"

    def test_no_metadata_returns_empty(self):
        meta = _parse_metadata("<html><head></head><body>no meta</body></html>")
        assert meta.description is None
        assert meta.title is None
        assert not meta.is_useful()

    def test_is_useful_requires_description(self):
        # Title-only is not enough — enrichment can do nothing useful
        # with only a title (matches the EmptyContentError precondition).
        meta = _parse_metadata("<html><head><title>Just a Title</title></head></html>")
        assert meta.title == "Just a Title"
        assert meta.description is None
        assert not meta.is_useful()

    def test_image_src_alias_for_twitter(self):
        # Some sites use twitter:image:src instead of twitter:image.
        html = """
        <html><head>
          <meta name="twitter:description" content="x">
          <meta name="twitter:image:src" content="https://cdn.example.com/tw.jpg">
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.image_url == "https://cdn.example.com/tw.jpg"

    def test_strips_whitespace_in_content(self):
        html = """
        <html><head>
          <meta property="og:title" content="   Spaced Title   ">
          <meta property="og:description" content="
              Multiline
              description with leading whitespace.
          ">
        </head></html>
        """
        meta = _parse_metadata(html)
        assert meta.title == "Spaced Title"
        assert meta.description.startswith("Multiline")

    def test_garbage_html_returns_empty(self):
        # Selectolax is permissive; even outright garbage shouldn't raise.
        meta = _parse_metadata("<<< not really html >>>")
        assert isinstance(meta, OGMetadata)
        assert not meta.is_useful()


# ---- fetch_og_metadata (HTTP integration) ------------------------------

def _mock_transport(handler):
    """Build an httpx AsyncClient backed by a request handler."""
    return httpx.MockTransport(handler)


class TestFetchOGMetadataHTTP:
    def test_happy_path(self):
        body = b"""
        <html><head>
          <meta property="og:title" content="Article">
          <meta property="og:description" content="Body of the article.">
        </head></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "Mozilla" in request.headers.get("user-agent", "")
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})

        async def go():
            async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
                return await fetch_og_metadata("https://example.com/x", client=client)

        meta = asyncio.run(go())
        assert meta is not None
        assert meta.is_useful()
        assert meta.title == "Article"
        assert meta.source == "og"

    def test_non_200_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"")

        async def go():
            async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
                return await fetch_og_metadata("https://example.com/x", client=client)

        assert asyncio.run(go()) is None

    def test_network_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async def go():
            async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
                return await fetch_og_metadata("https://example.com/x", client=client)

        assert asyncio.run(go()) is None

    def test_non_http_url_returns_none(self):
        # No fetch attempted — guard runs first.
        async def go():
            return await fetch_og_metadata("tg://message/123")

        assert asyncio.run(go()) is None

    def test_empty_body_returns_empty_meta(self):
        # 200 but no content — we get an OGMetadata with is_useful=False,
        # not None (the request itself succeeded).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"", headers={"content-type": "text/html"})

        async def go():
            async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
                return await fetch_og_metadata("https://example.com/x", client=client)

        meta = asyncio.run(go())
        assert meta is not None
        assert not meta.is_useful()

    def test_oversized_body_is_truncated(self):
        # 1 MB of garbage with the OG tags at the start. Parser should
        # still find them because we cap reads at 256 KB and the head
        # block is at byte 0.
        head = (
            b"<html><head>"
            b'<meta property="og:title" content="Big Page">'
            b'<meta property="og:description" content="Has tags up front.">'
            b"</head><body>"
        )
        body = head + (b"x" * (1024 * 1024)) + b"</body></html>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})

        async def go():
            async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
                return await fetch_og_metadata("https://example.com/big", client=client)

        meta = asyncio.run(go())
        assert meta is not None and meta.is_useful()
        assert meta.title == "Big Page"
