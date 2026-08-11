import statistics

import pytest
from worker_app.main import _handle_failure
from worker_app.retry import compute_backoff_delay
from worker_app.scraper import RetryableScrapeError

from shared.config import settings
from shared.models import Product
from shared.queue import dead_letter_depth, list_dead_letters


def _use_default_backoff_settings(monkeypatch, *, max_delay=600.0):
    monkeypatch.setattr(settings, "retry_base_delay_seconds", 5.0)
    monkeypatch.setattr(settings, "retry_backoff_factor", 2.0)
    monkeypatch.setattr(settings, "retry_max_delay_seconds", max_delay)


def test_backoff_ranges_grow_with_attempt_number(monkeypatch):
    _use_default_backoff_settings(monkeypatch)

    for _ in range(500):
        assert 0 <= compute_backoff_delay(1) <= 5
    for _ in range(500):
        assert 0 <= compute_backoff_delay(2) <= 10
    for _ in range(500):
        assert 0 <= compute_backoff_delay(3) <= 20


def test_backoff_capped_at_max_delay(monkeypatch):
    # base=5, factor=2 would put attempt 3 at 20 and attempt 4 at 40
    # uncapped — both should be clamped down to max_delay=15.
    _use_default_backoff_settings(monkeypatch, max_delay=15.0)

    for _ in range(200):
        assert 0 <= compute_backoff_delay(3) <= 15
    for _ in range(200):
        assert 0 <= compute_backoff_delay(4) <= 15

    # Confirm the cap actually raised attempt 3's ceiling above what an
    # uncapped attempt-2 range (10) would allow, i.e. we're really testing
    # the cap kicking in, not just a narrow range that happens to fit.
    samples = [compute_backoff_delay(3) for _ in range(500)]
    assert max(samples) > 10


def test_jitter_produces_varied_values(monkeypatch):
    _use_default_backoff_settings(monkeypatch)

    samples = [compute_backoff_delay(3) for _ in range(500)]  # drawn from [0, 20]
    assert len(set(samples)) > 490  # continuous distribution, should be ~all distinct
    assert min(samples) < 2  # some draws land near the bottom of the range
    assert max(samples) > 18  # some draws land near the top of the range
    assert statistics.pstdev(samples) > 3  # meaningfully spread out, not clustered


@pytest.mark.asyncio
async def test_max_attempts_respected_exactly(monkeypatch, db_session):
    """5 attempts, then dead-lettered — not 4 (stopping early) and not 6
    (running over budget)."""
    monkeypatch.setattr(settings, "retry_max_attempts", 5)

    product = Product(
        name="Test product", daraz_url="https://www.daraz.pk/products/test-i1-s1.html"
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    job = {
        "job_id": "test-job-max-attempts",
        "product_id": product.id,
        "url": product.daraz_url,
        "attempt": 1,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }

    for expected_attempt in range(1, 5):
        assert job["attempt"] == expected_attempt
        await _handle_failure(job, RetryableScrapeError("simulated"), started=0.0)
        assert await dead_letter_depth() == 0, (
            f"dead-lettered too early, after attempt {expected_attempt}"
        )

    assert job["attempt"] == 5
    await _handle_failure(job, RetryableScrapeError("simulated"), started=0.0)
    assert await dead_letter_depth() == 1

    [record] = await list_dead_letters()
    assert len(record["attempts"]) == 5
