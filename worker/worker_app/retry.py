# The backoff computation lives in shared/retry.py because shared/notifiers.py
# (alert email/webhook delivery) needs the same retry math the scraper's
# retry logic uses — one implementation, not two. This module re-exports it
# so worker code can `from .retry import compute_backoff_delay` per the
# project's file layout.
from shared.retry import compute_backoff_delay  # noqa: F401
