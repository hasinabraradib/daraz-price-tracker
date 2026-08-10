from fastapi import FastAPI
from sqlalchemy import text

from app.database import engine

app = FastAPI(title="Daraz Price Tracker")


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "unavailable"

    return {"status": "ok", "database": db_status}
