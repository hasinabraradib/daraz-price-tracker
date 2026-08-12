from fastapi import FastAPI
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
