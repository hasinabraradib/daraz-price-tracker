from fastapi import FastAPI
from sqlalchemy import text

from shared.database import engine

from app.routers import dead_letters, products, queue, stats

app = FastAPI(title="Daraz Price Tracker")

app.include_router(products.router)
app.include_router(queue.router)
app.include_router(dead_letters.router)
app.include_router(stats.router)


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "unavailable"

    return {"status": "ok", "database": db_status}
