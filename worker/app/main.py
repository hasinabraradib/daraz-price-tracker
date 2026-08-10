import asyncio
import json
import logging
import sys
import time
import uuid

from shared.database import async_session_factory
from shared.models import PriceSnapshot

from app.queue import dequeue_job
from app.scraper import ScrapeError, scrape_product

DEQUEUE_TIMEOUT_SECONDS = 5

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
logger = logging.getLogger("worker")


def log_event(**fields) -> None:
    logger.info(json.dumps(fields, default=str))


async def process_job(job: dict) -> None:
    job_id = uuid.uuid4().hex[:8]
    product_id = job["product_id"]
    url = job["url"]
    started_at = time.monotonic()

    try:
        result = await scrape_product(url)
    except ScrapeError as exc:
        log_event(
            job_id=job_id,
            product_id=product_id,
            outcome="failed",
            reason=str(exc),
            duration_seconds=round(time.monotonic() - started_at, 2),
        )
        return
    except Exception as exc:
        # Unexpected failure: log and move on. Retries are a later phase.
        log_event(
            job_id=job_id,
            product_id=product_id,
            outcome="error",
            reason=repr(exc),
            duration_seconds=round(time.monotonic() - started_at, 2),
        )
        return

    async with async_session_factory() as session:
        snapshot = PriceSnapshot(
            product_id=product_id,
            price=result.price,
            currency=result.currency,
            in_stock=result.in_stock,
            raw_title=result.title,
        )
        session.add(snapshot)
        await session.commit()

    log_event(
        job_id=job_id,
        product_id=product_id,
        outcome="success",
        price=str(result.price),
        currency=result.currency,
        in_stock=result.in_stock,
        duration_seconds=round(time.monotonic() - started_at, 2),
    )


async def run() -> None:
    log_event(event="worker_started")
    while True:
        job = await dequeue_job(timeout=DEQUEUE_TIMEOUT_SECONDS)
        if job is None:
            continue
        await process_job(job)


if __name__ == "__main__":
    asyncio.run(run())
