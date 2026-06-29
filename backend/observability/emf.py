"""CloudWatch EMF (Embedded Metric Format) emitter.

Why raw JSON, not aws-embedded-metrics-python
---------------------------------------------
The official library wraps the same JSON we write below and adds a
buffered-flusher with at-exit hooks. We don't need the queueing — each
request emits one self-contained line — and the dep would be ~5 MB of
asyncio glue for ~30 lines of code we control here. Trade is captured
in phase4.0.6.1-polish-design.md §4.

EMF spec (TL;DR):
    Every log line that contains the `_aws` block is parsed by
    CloudWatch at ingestion. The block declares (1) the namespace,
    (2) which top-level keys are dimensions (always present, used as
    filter axes), and (3) which top-level keys are metrics (numeric
    values with units). The rest of the JSON is dimension+metric data.
    See https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html

Sink: stdout. In production the docker-compose `awslogs` driver
streams stdout to the `/braintwin/app` log group. Locally, the line
just shows up in your terminal — useful for spot-checks and tests.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "BrainTwin/App"


def emit_metric(
    *,
    namespace: str = DEFAULT_NAMESPACE,
    dimensions: Mapping[str, str],
    metrics: Mapping[str, float],
    units: Mapping[str, str] | None = None,
) -> None:
    """Write one EMF JSON line to stdout.

    The function is sync and best-effort: any error during JSON encoding
    or stdout write is swallowed (with a debug log) so a metrics-system
    failure can never break the request path. This matches the official
    EMF lib's behaviour.

    Args:
        namespace: CloudWatch namespace (e.g., "BrainTwin/App").
        dimensions: filter axes that always appear with the metric. Keep
            cardinality bounded; high-cardinality dimensions like raw
            paths (`/capture/abc123`) explode the metric table. Use the
            ROUTE TEMPLATE (`/capture/{id}`) instead.
        metrics: name → value. Each value must be a float (the EMF spec
            also accepts arrays, but we don't use that here — keeps the
            wire shape simple).
        units: optional name → unit string. Defaults to "None" (which
            CloudWatch treats as unitless count-like). Use
            "Milliseconds" for latencies, "Count" for counters.
    """
    units = units or {}
    try:
        line: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": namespace,
                        # EMF expects a list-of-dimension-sets; we use
                        # exactly one set (all dimensions together).
                        # Using multiple sets explodes the metric count
                        # (one metric per set), which we don't need.
                        "Dimensions": [list(dimensions.keys())]
                        if dimensions
                        else [[]],
                        "Metrics": [
                            {"Name": name, "Unit": units.get(name, "None")}
                            for name in metrics
                        ],
                    }
                ],
            },
        }
        line.update(dimensions)
        # CloudWatch requires metric values to be numeric. Cast in case
        # the caller passed an int.
        for name, value in metrics.items():
            line[name] = float(value)

        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        # Never raise from the metrics path. A noisy debug log so a
        # developer can find the failure if they're looking; production
        # error logs stay clean.
        logger.debug("EMF emit failed", exc_info=True)


@asynccontextmanager
async def timed(
    metric_name: str,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    dimensions: Mapping[str, str] | None = None,
    extra_metrics: Mapping[str, float] | None = None,
) -> AsyncIterator[None]:
    """Time an async code block; emit one EMF line on exit.

    Adds an `error` dimension automatically — "none" on success,
    `type(exc).__name__` on raised. This lets a CloudWatch alarm
    filter on `error != "none"` to count failures per call site
    without a separate counter metric.

    Usage:
        async with timed("anthropic_latency_ms",
                         dimensions={"endpoint": "enrich", "model": "haiku-3-5"}):
            await client.messages.create(...)
    """
    start = time.monotonic()
    error_type = "none"
    try:
        yield
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        dims: dict[str, str] = dict(dimensions or {})
        dims["error"] = error_type
        # Build a metrics dict whose first key is the timing metric
        # (in milliseconds). Extra metrics are passed through unit-less.
        all_metrics: dict[str, float] = {metric_name: elapsed_ms}
        if extra_metrics:
            all_metrics.update(extra_metrics)
        units = {metric_name: "Milliseconds"}
        for name in extra_metrics or {}:
            units[name] = "Count"
        emit_metric(
            namespace=namespace,
            dimensions=dims,
            metrics=all_metrics,
            units=units,
        )


class EMFMiddleware:
    """Starlette/ASGI middleware: one EMF line per HTTP request.

    Emits:
        latency_ms (Milliseconds) — request handler wall-clock time
        count (Count) — always 1 (lets CW dashboards graph request rate)

    Dimensions:
        route   — the matched route TEMPLATE (e.g., "/capture",
                  not "/capture/abc123"). Falls back to scope["path"]
                  if Starlette didn't attach a route to the scope.
        method  — "GET" / "POST" / ...
        status  — "200" / "404" / "500" / ...

    Non-HTTP scopes (websocket, lifespan) pass through untouched.

    Mounted as a standard FastAPI middleware:
        app.add_middleware(EMFMiddleware)
    """

    def __init__(self, app: Any, namespace: str = DEFAULT_NAMESPACE) -> None:
        self.app = app
        self.namespace = namespace

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "")
        # Capture the response status from the first http.response.start.
        # If the handler crashes before sending one, status stays 500
        # (matches what Starlette emits to the client).
        status_holder = {"status": 500}

        async def send_wrapper(message: Mapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        start = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = (time.monotonic() - start) * 1000.0
            route = self._route_template(scope, path)
            emit_metric(
                namespace=self.namespace,
                dimensions={
                    "route": route,
                    "method": method,
                    "status": str(status_holder["status"]),
                },
                metrics={"latency_ms": latency_ms, "count": 1.0},
                units={"latency_ms": "Milliseconds", "count": "Count"},
            )

    @staticmethod
    def _route_template(scope: Mapping[str, Any], fallback_path: str) -> str:
        """Return a BOUNDED-cardinality route label for the metric.

        We run as an outer ASGI middleware, so we re-read the scope at
        finally-time after the router has matched. Starlette mutates the
        scope in place during routing, but on the version we pin it sets
        only `endpoint` and `path_params` — NOT `route`. So:

          - endpoint present  → a route matched. Every current route is
            static (`/capture`, `/recall`, …), so the raw path already
            IS the template and is safe to use as a dimension value.
          - endpoint absent   → nothing matched (404 / scanner probe at
            `/.env`, `/wp-admin`, random junk). Using the raw path here
            would let internet noise mint one CloudWatch custom metric
            per unique path — unbounded cardinality and cost. Clamp it
            to a single sentinel instead.

        NOTE: if a parameterised route (`/capture/{id}`) is ever added,
        the matched branch must derive the template from `app.routes`
        (`route.matches(scope)` → `route.path`). The raw path alone
        would re-introduce the explosion for that route. Do NOT trust
        `scope["route"]` — this Starlette version never sets it."""
        if scope.get("endpoint") is None:
            return "<unmatched>"
        return fallback_path
