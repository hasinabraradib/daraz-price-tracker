from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from shared.metrics import refresh_gauges

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    # Awaited directly in this async handler, so the API's gauges are
    # refreshed at exactly scrape time, every time — see
    # shared/metrics.py's module docstring for why gauges need this at
    # all and how the worker (no per-request hook available) differs.
    await refresh_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
