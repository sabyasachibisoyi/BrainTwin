"""Observability — application-side instrumentation for CloudWatch.

Phase 4.0.6.1 M.11. Emits CloudWatch Embedded Metric Format (EMF) log
lines from inside the app process. The `awslogs` driver on the
container streams stdout to the `/braintwin/app` CloudWatch log group;
CloudWatch parses the EMF block at log ingestion and extracts the
metrics into the `BrainTwin/App` namespace. No additional API calls,
no separate metric-publishing path.

Public surface:
    - EMFMiddleware     — Starlette ASGI middleware (per-request)
    - timed             — async context manager for ad-hoc spans
    - emit_metric       — raw escape hatch
"""

from backend.observability.emf import (
    EMFMiddleware,
    emit_metric,
    timed,
)

__all__ = ["EMFMiddleware", "emit_metric", "timed"]
