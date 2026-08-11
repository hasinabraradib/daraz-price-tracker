import random

from shared.config import settings


def compute_backoff_delay(attempt_number: int) -> float:
    """Full-jitter exponential backoff for the delay before retrying, given
    that `attempt_number` (1-indexed) just failed.

    computed_delay = min(base * factor^(attempt_number - 1), max_delay)
    actual_delay   = random.uniform(0, computed_delay)
    """
    computed_delay = min(
        settings.retry_base_delay_seconds
        * (settings.retry_backoff_factor ** (attempt_number - 1)),
        settings.retry_max_delay_seconds,
    )
    return random.uniform(0, computed_delay)
