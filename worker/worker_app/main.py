import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from prometheus_client import start_http_server

from shared.alerts import evaluate_alerts
from shared.config import settings
from shared.database import async_session_factory
from shared.metrics import (
    JOBS_DEAD_LETTERED_TOTAL,
    SCRAPE_ATTEMPTS_TOTAL,
    SCRAPE_DURATION_SECONDS,
    refresh_gauges,
)
from shared.models import PriceSnapshot, ScrapeAttempt

from .queue import dead_letter, dequeue_job, promote_due_jobs, schedule_retry
from .retry import compute_backoff_delay
from .scraper import (
    ScrapedProduct,
    ScrapeError,
    SelectorScrapeError,
    TerminalScrapeError,
    scrape_product,
)

METRICS_PORT = 9100

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    stream=sys.stdout,
    format="%(message)s",
)
logger = logging.getLogger("worker")

# Set on SIGTERM (see run()). Kubernetes sends SIGTERM before deleting a
# pod, then SIGKILL after terminationGracePeriodSeconds if the process is
# still alive. Without trapping it, Python's default disposition kills the
# process immediately — including mid-scrape, abandoning the job with no
# record of what happened to it (dequeue_job doesn't leave an in-flight
# copy anywhere). Trapping SIGTERM lets each loop finish its *current*
# iteration (i.e. let process_job() return) before exiting, so a grace
# period long enough for one scrape actually protects that scrape.
shutdown_event = asyncio.Event()


def log_event(**fields) -> None:
    logger.info(json.dumps(fields, default=str))


async def _record_attempt(
    product_id: int,
    *,
    success: bool,
    error_type: str | None,
    error_message: str | None,
    duration_ms: int,
    attempt_number: int,
) -> None:
    async with async_session_factory() as session:
        session.add(
            ScrapeAttempt(
                product_id=product_id,
                success=success,
                error_type=error_type,
                error_message=error_message,
                duration_ms=duration_ms,
                attempt_number=attempt_number,
            )
        )
        await session.commit()


async def _handle_success(job: dict, result: ScrapedProduct, started: float) -> None:
    job_id = job["job_id"]
    product_id = job["product_id"]
    attempt_number = job["attempt"]
    duration_ms = int((time.monotonic() - started) * 1000)

    SCRAPE_ATTEMPTS_TOTAL.labels(outcome="success", error_type="").inc()
    SCRAPE_DURATION_SECONDS.observe(duration_ms / 1000)

    async with async_session_factory() as session:
        session.add(
            PriceSnapshot(
                product_id=product_id,
                price=result.price,
                currency=result.currency,
                in_stock=result.in_stock,
                raw_title=result.title,
            )
        )
        session.add(
            ScrapeAttempt(
                product_id=product_id,
                success=True,
                error_type=None,
                error_message=None,
                duration_ms=duration_ms,
                attempt_number=attempt_number,
            )
        )
        await session.commit()

    async with async_session_factory() as alert_session:
        alert_events = await evaluate_alerts(alert_session, product_id)

    log_event(
        job_id=job_id,
        product_id=product_id,
        outcome="success",
        price=str(result.price),
        currency=result.currency,
        in_stock=result.in_stock,
        duration_ms=duration_ms,
        attempt_number=attempt_number,
        alert_events=len(alert_events),
    )


async def _handle_failure(job: dict, exc: ScrapeError, started: float) -> None:
    job_id = job["job_id"]
    product_id = job["product_id"]
    attempt_number = job["attempt"]
    duration_ms = int((time.monotonic() - started) * 1000)
    error_type = type(exc).__name__
    error_message = str(exc)

    SCRAPE_ATTEMPTS_TOTAL.labels(outcome="failure", error_type=error_type).inc()
    SCRAPE_DURATION_SECONDS.observe(duration_ms / 1000)

    await _record_attempt(
        product_id,
        success=False,
        error_type=error_type,
        error_message=error_message,
        duration_ms=duration_ms,
        attempt_number=attempt_number,
    )

    # Read the *previous* attempt's error type before we append this one —
    # a SelectorScrapeError only gets one retry. If the attempt before this
    # one also failed on selectors, Daraz's markup almost certainly changed
    # and another retry won't help, so we dead-letter now instead of
    # burning the rest of the attempt budget.
    history = job.get("attempt_history", [])
    previous_was_selector_error = (
        bool(history) and history[-1]["error_type"] == SelectorScrapeError.__name__
    )

    history.append(
        {
            "attempt_number": attempt_number,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "error_type": error_type,
            "error_message": error_message,
        }
    )
    job["attempt_history"] = history

    if isinstance(exc, TerminalScrapeError):
        reason = "terminal"
    elif isinstance(exc, SelectorScrapeError) and previous_was_selector_error:
        reason = "repeated_selector_failure"
    elif attempt_number >= settings.retry_max_attempts:
        reason = "attempts_exhausted"
    else:
        reason = None

    if reason is not None:
        await dead_letter(job, final_error_type=error_type, final_error_message=error_message)
        JOBS_DEAD_LETTERED_TOTAL.labels(reason=reason).inc()
        log_event(
            job_id=job_id,
            product_id=product_id,
            outcome="dead_lettered",
            reason=reason,
            error_type=error_type,
            attempt_number=attempt_number,
            duration_ms=duration_ms,
        )
        return

    job["attempt"] = attempt_number + 1
    delay = compute_backoff_delay(attempt_number)
    await schedule_retry(job, delay_seconds=delay)
    log_event(
        job_id=job_id,
        product_id=product_id,
        outcome="retry_scheduled",
        error_type=error_type,
        attempt_number=attempt_number,
        next_attempt=attempt_number + 1,
        delay_seconds=round(delay, 2),
        duration_ms=duration_ms,
    )


async def _handle_unclassified_failure(job: dict, exc: Exception, started: float) -> None:
    # Anything that isn't a ScrapeError is a bug in our own code, not a
    # known scrape failure mode. Treat it conservatively as non-retryable
    # so a bug can't spin a job forever — it still lands in the DLQ with
    # full context for investigation.
    job_id = job["job_id"]
    product_id = job["product_id"]
    attempt_number = job["attempt"]
    duration_ms = int((time.monotonic() - started) * 1000)
    error_type = f"Unclassified.{type(exc).__name__}"
    error_message = repr(exc)

    SCRAPE_ATTEMPTS_TOTAL.labels(outcome="failure", error_type=error_type).inc()
    SCRAPE_DURATION_SECONDS.observe(duration_ms / 1000)

    await _record_attempt(
        product_id,
        success=False,
        error_type=error_type,
        error_message=error_message,
        duration_ms=duration_ms,
        attempt_number=attempt_number,
    )

    history = job.get("attempt_history", [])
    history.append(
        {
            "attempt_number": attempt_number,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "error_type": error_type,
            "error_message": error_message,
        }
    )
    job["attempt_history"] = history

    await dead_letter(job, final_error_type=error_type, final_error_message=error_message)
    JOBS_DEAD_LETTERED_TOTAL.labels(reason="unclassified_error").inc()
    log_event(
        job_id=job_id,
        product_id=product_id,
        outcome="dead_lettered",
        reason="unclassified_error",
        error_type=error_type,
        attempt_number=attempt_number,
        duration_ms=duration_ms,
    )


async def process_job(job: dict) -> None:
    started = time.monotonic()
    try:
        result = await scrape_product(job["url"])
    except ScrapeError as exc:
        await _handle_failure(job, exc, started)
        return
    except Exception as exc:
        await _handle_unclassified_failure(job, exc, started)
        return

    await _handle_success(job, result, started)


async def worker_loop() -> None:
    while not shutdown_event.is_set():
        job = await dequeue_job(timeout=settings.dequeue_timeout_seconds)
        if job is None:
            continue
        await process_job(job)


async def promoter_loop() -> None:
    """Periodically move delayed jobs whose backoff has elapsed back onto
    the main queue. Polling, not sleeping out the backoff itself — the
    delay lives in Redis so it survives worker restarts."""
    while not shutdown_event.is_set():
        moved = await promote_due_jobs()
        if moved:
            log_event(event="promoted_delayed_jobs", count=moved)
        await asyncio.sleep(settings.promote_interval_seconds)


async def metrics_refresh_loop() -> None:
    """The worker has no per-request hook to refresh gauges at exact scrape
    time the way the API's GET /metrics handler does (see
    shared/metrics.py's module docstring for the full reasoning) —
    prometheus_client's start_http_server just serves whatever the gauges
    were last set to. So we refresh them here on a timer instead."""
    while not shutdown_event.is_set():
        await refresh_gauges()
        await asyncio.sleep(settings.gauge_refresh_interval_seconds)


async def run() -> None:
    start_http_server(METRICS_PORT)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    log_event(event="worker_started", metrics_port=METRICS_PORT)
    await asyncio.gather(worker_loop(), promoter_loop(), metrics_refresh_loop())
    log_event(event="worker_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(run())
