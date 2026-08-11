"""Delivers AlertEvent notifications by email or webhook.

Retries transient failures using the exact same backoff math the scraper's
retry logic uses (shared/retry.py's compute_backoff_delay) — not a second
implementation. Unlike the scraper's retries, which are persisted to Redis
so they survive a worker restart, notification retries just `asyncio.sleep`
in place: a notification send is a single quick step at the end of
processing one job, not something worth the complexity of a durable retry
queue. Because worker_loop processes jobs one at a time, a slow retry here
does delay picking up the *next* scrape job — an accepted tradeoff for this
foundation, not something to fix without being asked.
"""
import asyncio
import logging
from email.message import EmailMessage

import aiosmtplib
import httpx

from shared.config import settings
from shared.retry import compute_backoff_delay

logger = logging.getLogger("notifiers")

NOTIFY_MAX_ATTEMPTS = 3
WEBHOOK_TIMEOUT_SECONDS = 10


class NotifyError(Exception):
    """Raised when a notification could not be delivered after retries."""


async def _send_email(destination: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.alert_from_email
    message["To"] = destination
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_use_tls,
    )


async def _send_webhook(destination: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        response = await client.post(destination, json=payload)
        response.raise_for_status()


async def send_notification(
    channel: str, destination: str, subject: str, body: str, payload: dict
) -> None:
    """Send via `channel` ("email" or "webhook"), retrying transient
    failures with full-jitter backoff. Raises NotifyError if every attempt
    fails."""
    last_error: Exception | None = None
    for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
        try:
            if channel == "email":
                await _send_email(destination, subject, body)
            elif channel == "webhook":
                await _send_webhook(destination, payload)
            else:
                raise NotifyError(f"unknown notification channel: {channel!r}")
            return
        except NotifyError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "notification attempt %s/%s failed (channel=%s): %s",
                attempt, NOTIFY_MAX_ATTEMPTS, channel, exc,
            )
            if attempt < NOTIFY_MAX_ATTEMPTS:
                await asyncio.sleep(compute_backoff_delay(attempt))

    raise NotifyError(str(last_error)) from last_error
