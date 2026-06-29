"""Tests for backend.observability.emf — Phase 4.0.6.1 M.11.

What we care about:

  - `emit_metric` writes a well-formed EMF JSON line: required keys
    present, dimensions echoed at top level, metric values numeric.
  - `timed` emits one line on success with error="none", and one line
    on raise with error=<exception class name>. The exception MUST
    still propagate to the caller.
  - The EMF emitter is best-effort: a JSON-serialization failure (e.g.,
    a non-string in dimensions) does NOT raise.
  - `EMFMiddleware` emits one line per HTTP request with the expected
    dimensions (route, method, status) and `latency_ms` > 0.
  - Non-HTTP scopes pass through without emitting.

The metric values we don't strongly assert on (latency_ms is wall-clock,
flaky in CI). We assert presence + type + sign — that's what the
dashboard ultimately reads.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from typing import Any

import pytest

from backend.observability.emf import (
    DEFAULT_NAMESPACE,
    EMFMiddleware,
    emit_metric,
    timed,
)


# --- Helpers --------------------------------------------------------


def _capture_stdout() -> io.StringIO:
    """Replace sys.stdout with a StringIO so we can read what was written."""
    buf = io.StringIO()
    sys.stdout = buf
    return buf


def _restore_stdout() -> None:
    sys.stdout = sys.__stdout__


def _read_emf_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    """Parse every EMF line written to buf back into dicts."""
    raw = buf.getvalue()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# --- emit_metric ----------------------------------------------------


def test_emit_metric_writes_well_formed_emf_json():
    buf = _capture_stdout()
    try:
        emit_metric(
            namespace="BrainTwin/App",
            dimensions={"route": "/capture", "method": "POST", "status": "200"},
            metrics={"latency_ms": 12.3, "count": 1},
            units={"latency_ms": "Milliseconds", "count": "Count"},
        )
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)

    # Required EMF block
    assert "_aws" in line
    aws = line["_aws"]
    assert isinstance(aws["Timestamp"], int)
    [cw] = aws["CloudWatchMetrics"]
    assert cw["Namespace"] == "BrainTwin/App"
    assert cw["Dimensions"] == [["route", "method", "status"]]
    metric_names = {m["Name"] for m in cw["Metrics"]}
    assert metric_names == {"latency_ms", "count"}
    units_by_name = {m["Name"]: m["Unit"] for m in cw["Metrics"]}
    assert units_by_name == {"latency_ms": "Milliseconds", "count": "Count"}

    # Dimensions echoed at top level
    assert line["route"] == "/capture"
    assert line["method"] == "POST"
    assert line["status"] == "200"
    # Metric values numeric and float-typed
    assert isinstance(line["latency_ms"], float)
    assert line["latency_ms"] == pytest.approx(12.3)
    assert isinstance(line["count"], float)
    assert line["count"] == 1.0


def test_emit_metric_default_namespace_is_braintwin_app():
    buf = _capture_stdout()
    try:
        emit_metric(dimensions={"x": "1"}, metrics={"v": 0})
    finally:
        _restore_stdout()
    [line] = _read_emf_lines(buf)
    [cw] = line["_aws"]["CloudWatchMetrics"]
    assert cw["Namespace"] == DEFAULT_NAMESPACE == "BrainTwin/App"


def test_emit_metric_swallows_errors_silently():
    """A JSON-encoder failure (e.g., a non-serializable dimension value)
    must NOT raise — observability failures cannot break the request
    path. We pass a non-stringifiable object as a dimension value and
    expect a silent no-op."""

    class NotJsonSerializable:
        pass

    buf = _capture_stdout()
    try:
        emit_metric(
            dimensions={"weird": NotJsonSerializable()},  # type: ignore[dict-item]
            metrics={"v": 1.0},
        )
    finally:
        _restore_stdout()

    # The line did NOT get written (encoding failed silently).
    lines = _read_emf_lines(buf)
    assert lines == []


# --- timed ----------------------------------------------------------


def test_timed_emits_one_line_on_success_with_error_none():
    async def _run() -> None:
        async with timed(
            "anthropic_latency_ms",
            dimensions={"endpoint": "enrich", "model": "haiku-3-5"},
        ):
            await asyncio.sleep(0.001)  # ensure latency_ms > 0

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    assert line["endpoint"] == "enrich"
    assert line["model"] == "haiku-3-5"
    assert line["error"] == "none"
    # latency is positive but not strictly bounded — wall-clock flake.
    assert isinstance(line["anthropic_latency_ms"], float)
    assert line["anthropic_latency_ms"] > 0.0


def test_timed_emits_with_error_dim_and_reraises():
    """When the wrapped block raises, the EMF line records the exception
    type in the `error` dimension AND the exception propagates to the
    caller. This is the dashboard's error-rate signal."""

    async def _run() -> None:
        async with timed(
            "anthropic_latency_ms",
            dimensions={"endpoint": "enrich", "model": "haiku-3-5"},
        ):
            raise ValueError("boom")

    buf = _capture_stdout()
    try:
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    assert line["error"] == "ValueError"
    assert line["endpoint"] == "enrich"
    # Latency is still recorded (we time before the raise).
    assert line["anthropic_latency_ms"] >= 0.0


def test_timed_extra_metrics_flow_through_as_counts():
    """A caller can pass extra non-timing metrics (e.g., input/output
    token counts) via `extra_metrics`. They get unit `Count`."""

    async def _run() -> None:
        async with timed(
            "anthropic_latency_ms",
            dimensions={"endpoint": "enrich", "model": "haiku-3-5"},
            extra_metrics={"input_tokens": 42, "output_tokens": 17},
        ):
            pass

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    [cw] = line["_aws"]["CloudWatchMetrics"]
    units = {m["Name"]: m["Unit"] for m in cw["Metrics"]}
    assert units["input_tokens"] == "Count"
    assert units["output_tokens"] == "Count"
    assert units["anthropic_latency_ms"] == "Milliseconds"
    assert line["input_tokens"] == 42.0
    assert line["output_tokens"] == 17.0


# --- EMFMiddleware --------------------------------------------------


class _DummyApp:
    """Minimal ASGI app that responds 200 with a tiny body. Tracks
    whether it was actually called so we know the middleware doesn't
    short-circuit valid requests."""

    def __init__(self, status: int = 200):
        self.status = status
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        # Mimic Starlette's router injecting the matched endpoint into
        # the scope in place — without it the middleware treats the
        # request as unmatched and clamps `route` to "<unmatched>".
        if scope.get("type") == "http":
            scope["endpoint"] = self
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def test_middleware_emits_one_line_per_http_request():
    app = _DummyApp(status=200)
    mw = EMFMiddleware(app)

    async def _run():
        scope = {"type": "http", "method": "POST", "path": "/capture"}
        await mw(scope, lambda: None, _noop_send())

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    assert app.called is True
    [line] = _read_emf_lines(buf)
    assert line["route"] == "/capture"
    assert line["method"] == "POST"
    assert line["status"] == "200"
    assert line["count"] == 1.0
    assert isinstance(line["latency_ms"], float)
    assert line["latency_ms"] >= 0.0


def test_middleware_records_500_when_handler_does_not_set_status():
    """If the handler crashes before sending http.response.start (i.e.,
    the middleware never sees a status), we default to 500 — which
    matches what Starlette would return to the client anyway."""

    class _CrashApp:
        async def __call__(self, scope, receive, send):
            # A real handler crash happens AFTER the router matched, so
            # the matched endpoint is already on the scope.
            scope["endpoint"] = self
            raise RuntimeError("oops")

    mw = EMFMiddleware(_CrashApp())

    async def _run():
        scope = {"type": "http", "method": "GET", "path": "/recall"}
        with pytest.raises(RuntimeError):
            await mw(scope, lambda: None, _noop_send())

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    assert line["status"] == "500"
    assert line["route"] == "/recall"


def test_middleware_clamps_unmatched_route_to_bound_cardinality():
    """A request that matches no route (404 / scanner probe) must NOT
    leak its raw path into the `route` dimension — that would mint one
    CloudWatch custom metric per unique path. Starlette leaves
    scope["endpoint"] unset when nothing matched, so we clamp to a
    single sentinel."""

    class _NotFoundApp:
        async def __call__(self, scope, receive, send):
            # Router matched nothing → no "endpoint" added to the scope.
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    mw = EMFMiddleware(_NotFoundApp())

    async def _run():
        scope = {"type": "http", "method": "GET", "path": "/.env"}
        await mw(scope, lambda: None, _noop_send())

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    assert line["route"] == "<unmatched>"
    assert line["status"] == "404"


def test_middleware_uses_path_when_route_matched():
    """When the router matches a route it injects scope["endpoint"] in
    place. Our current routes are all static, so the raw path is already
    the template — the middleware should emit it verbatim (not the
    sentinel)."""

    def _endpoint():  # stand-in for the matched route handler
        pass

    class _MatchedApp:
        async def __call__(self, scope, receive, send):
            # Mimic Starlette's router mutating the scope on a match.
            scope["endpoint"] = _endpoint
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    mw = EMFMiddleware(_MatchedApp())

    async def _run():
        scope = {"type": "http", "method": "POST", "path": "/capture"}
        await mw(scope, lambda: None, _noop_send())

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    [line] = _read_emf_lines(buf)
    assert line["route"] == "/capture"
    assert line["status"] == "200"


def test_middleware_passes_through_non_http_scopes_without_emitting():
    """websocket and lifespan scopes go to the app unchanged; no EMF
    line is written. This keeps the websocket handshake cost-free."""

    class _CountingApp:
        def __init__(self):
            self.called = False

        async def __call__(self, scope, receive, send):
            self.called = True

    counter = _CountingApp()
    mw = EMFMiddleware(counter)

    async def _run():
        scope = {"type": "websocket", "path": "/ws"}
        await mw(scope, lambda: None, _noop_send())

    buf = _capture_stdout()
    try:
        asyncio.run(_run())
    finally:
        _restore_stdout()

    assert counter.called is True
    assert _read_emf_lines(buf) == []


# --- ASGI test helper -----------------------------------------------


def _noop_send():
    """Return an awaitable send-callable that swallows messages."""

    async def _send(message):
        return None

    return _send
