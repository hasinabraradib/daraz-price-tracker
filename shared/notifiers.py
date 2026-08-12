"""Delivers AlertEvent notifications by email or webhook.

Discord webhook URLs (containing "discord.com/api/webhooks") are detected
automatically and get Discord's required `{"content": "..."}` shape instead
of the generic structured payload — no separate "channel" needed, an
AlertRule is still just `channel="webhook"` with `destination` set to the
Discord URL.

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
import time
from email.message import EmailMessage

import aiosmtplib
import httpx

from shared.config import settings
from shared.metrics import ALERT_DELIVERIES_TOTAL, ALERT_DELIVERY_DURATION_SECONDS
from shared.retry import compute_backoff_delay

logger = logging.getLogger("notifiers")

NOTIFY_MAX_ATTEMPTS = 3
WEBHOOK_TIMEOUT_SECONDS = 10
DISCORD_WEBHOOK_MARKER = "discord.com/api/webhooks"


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


async def _send_webhook(destination: str, body: str, payload: dict) -> None:
    # Discord requires the exact shape {"content": "..."} — a generic JSON
    # payload gets silently ignored (or rejected) otherwise. `body` is
    # already the same human-readable message used for the email body
    # (product name, what triggered, price change, product link — see
    # shared/alerts.py::_deliver), so it doubles as Discord content with
    # no separate formatting path to keep in sync.
    if DISCORD_WEBHOOK_MARKER in destination:
        json_payload = {"content": body}
    else:
        json_payload = payload

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        response = await client.post(destination, json=json_payload)
        response.raise_for_status()


async def send_notification(
    channel: str, destination: str, subject: str, body: str, payload: dict
) -> None:
    """Send via `channel` ("email" or "webhook"), retrying transient
    failures with full-jitter backoff. Raises NotifyError if every attempt
    fails. Records the delivery outcome/duration metrics around whatever
    `_send_with_retries` does, so those two concerns stay easy to read
    separately."""
    started = time.monotonic()
    try:
        await _send_with_retries(channel, destination, subject, body, payload)
    except NotifyError:
        ALERT_DELIVERIES_TOTAL.labels(channel=channel, status="failed").inc()
        raise
    else:
        ALERT_DELIVERIES_TOTAL.labels(channel=channel, status="sent").inc()
    finally:
        ALERT_DELIVERY_DURATION_SECONDS.observe(time.monotonic() - started)


async def _send_with_retries(
    channel: str, destination: str, subject: str, body: str, payload: dict
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
        try:
            if channel == "email":
                await _send_email(destination, subject, body)
            elif channel == "webhook":
                await _send_webhook(destination, body, payload)
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
