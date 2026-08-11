from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_db
from shared.models import ScrapeAttempt
from shared.queue import dead_letter_depth, delayed_queue_depth
from shared.queue import queue_depth as get_queue_depth

from app.schemas import ScrapeHealthStats

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/scrape-health", response_model=ScrapeHealthStats)
async def scrape_health(db: AsyncSession = Depends(get_db)):
    total = (
        await db.execute(select(func.count()).select_from(ScrapeAttempt))
    ).scalar_one()
    successes = (
        await db.execute(
            select(func.count())
            .select_from(ScrapeAttempt)
            .where(ScrapeAttempt.success.is_(True))
        )
    ).scalar_one()

    failures_stmt = (
        select(ScrapeAttempt.error_type, func.count())
        .where(ScrapeAttempt.success.is_(False))
        .group_by(ScrapeAttempt.error_type)
    )
    failures_by_error_type = {
        (error_type or "unknown"): count
        for error_type, count in (await db.execute(failures_stmt)).all()
    }

    return ScrapeHealthStats(
        total_attempts=total,
        success_rate=(successes / total) if total else None,
        failures_by_error_type=failures_by_error_type,
        queue_depth=await get_queue_depth(),
        delayed_queue_depth=await delayed_queue_depth(),
        dead_letter_depth=await dead_letter_depth(),
    )
