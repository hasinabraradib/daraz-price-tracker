import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.routers import (
    alert_rules,
    alerts,
    competitors,
    dead_letters,
    metrics,
    products,
    queue,
    stats,
)
from shared.database import engine

app = FastAPI(title="Daraz Price Tracker")

# The web/ frontend runs on a different origin (Next.js dev server on
# :3000 by default) than this API (:8000), so the browser needs an
# explicit CORS allow before it'll let fetch() calls through.
# FRONTEND_ORIGIN is an env var rather than hardcoded so this doesn't
# silently break for anyone running the frontend on a different port/host
# (see web/README.md's NEXT_PUBLIC_API_URL for the other half of this
# pairing). X-Owner-Email is listed explicitly — it's a custom header, not
# one of the handful CORS treats as "simple" and allows by default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-Owner-Email"],
)

app.include_router(products.router)
app.include_router(queue.router)
app.include_router(dead_letters.router)
app.include_router(stats.router)
app.include_router(competitors.router)
app.include_router(alert_rules.router)
app.include_router(alerts.router)
app.include_router(metrics.router)


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "unavailable"

    return {"status": "ok", "database": db_status}


@app.get("/live")
async def live():
    # Deliberately does not touch the database. Used as the Kubernetes
    # liveness probe: liveness restarts the pod, and a pod restart doesn't
    # fix a database outage — it just kills every API pod at once while
    # they're all busy failing the same DB check, then restarts them into
    # the same outage. /health (which does check the DB) is the readiness
    # probe instead, so pods are pulled out of rotation without being
    # killed.
    return {"status": "ok"}
