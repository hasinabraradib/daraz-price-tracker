import pytest

from shared.queue import (
    dead_letter,
    dead_letter_depth,
    delayed_queue_depth,
    dequeue_job,
    enqueue_job,
    list_dead_letters,
    promote_due_jobs,
    purge_dead_letter,
    queue_depth,
    replay_dead_letter,
    schedule_retry,
)


@pytest.mark.asyncio
async def test_enqueue_dequeue_round_trip_preserves_payload():
    job_id = await enqueue_job(product_id=42, url="https://www.daraz.pk/products/x-i1-s1.html")

    job = await dequeue_job(timeout=1)

    assert job is not None
    assert job["job_id"] == job_id
    assert job["product_id"] == 42
    assert job["url"] == "https://www.daraz.pk/products/x-i1-s1.html"
    assert job["attempt"] == 1
    assert job["attempt_history"] == []
    assert "enqueued_at" in job


@pytest.mark.asyncio
async def test_dequeue_returns_none_when_empty():
    job = await dequeue_job(timeout=1)
    assert job is None


@pytest.mark.asyncio
async def test_queue_depth_accurate():
    assert await queue_depth() == 0

    await enqueue_job(product_id=1, url="https://www.daraz.pk/a-i1-s1.html")
    assert await queue_depth() == 1

    await enqueue_job(product_id=2, url="https://www.daraz.pk/b-i2-s2.html")
    assert await queue_depth() == 2

    await dequeue_job(timeout=1)
    assert await queue_depth() == 1


@pytest.mark.asyncio
async def test_dead_letter_push_list_purge():
    job = {
        "job_id": "job-abc",
        "product_id": 7,
        "url": "https://www.daraz.pk/x-i1-s1.html",
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [
            {
                "attempt_number": 1,
                "attempted_at": "2026-01-01T00:00:05+00:00",
                "error_type": "TerminalScrapeError",
                "error_message": "404",
            }
        ],
    }

    assert await dead_letter_depth() == 0

    returned_job_id = await dead_letter(
        job, final_error_type="TerminalScrapeError", final_error_message="404"
    )
    assert returned_job_id == "job-abc"
    assert await dead_letter_depth() == 1

    [record] = await list_dead_letters()
    assert record["job_id"] == "job-abc"
    assert record["original_job"]["product_id"] == 7
    assert record["original_job"]["url"] == job["url"]
    assert record["attempts"] == job["attempt_history"]
    assert record["final_error_type"] == "TerminalScrapeError"
    assert record["final_error_message"] == "404"

    purged = await purge_dead_letter("job-abc")
    assert purged is True
    assert await dead_letter_depth() == 0

    # purging again returns False — nothing there to remove
    assert await purge_dead_letter("job-abc") is False


@pytest.mark.asyncio
async def test_replay_puts_job_back_on_main_queue_with_attempt_reset():
    job = {
        "job_id": "job-to-replay",
        "product_id": 3,
        "url": "https://www.daraz.pk/x-i1-s1.html",
        "attempt": 5,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [
            {
                "attempt_number": n,
                "attempted_at": "2026-01-01T00:00:00+00:00",
                "error_type": "RetryableScrapeError",
                "error_message": "boom",
            }
            for n in range(1, 6)
        ],
    }
    await dead_letter(job, final_error_type="RetryableScrapeError", final_error_message="boom")
    assert await dead_letter_depth() == 1
    assert await queue_depth() == 0

    replayed = await replay_dead_letter("job-to-replay")
    assert replayed is True
    assert await dead_letter_depth() == 0
    assert await queue_depth() == 1

    requeued = await dequeue_job(timeout=1)
    assert requeued["job_id"] == "job-to-replay"
    assert requeued["product_id"] == 3
    assert requeued["url"] == job["url"]
    assert requeued["attempt"] == 1  # reset, not still 5
    assert requeued["attempt_history"] == []  # cleared


@pytest.mark.asyncio
async def test_replay_missing_job_id_returns_false():
    assert await replay_dead_letter("does-not-exist") is False


@pytest.mark.asyncio
async def test_promote_due_jobs_moves_only_jobs_whose_backoff_elapsed():
    due_job = {"job_id": "due", "product_id": 1, "url": "https://www.daraz.pk/a-i1-s1.html"}
    not_due_job = {
        "job_id": "not-due", "product_id": 2, "url": "https://www.daraz.pk/b-i2-s2.html"
    }

    await schedule_retry(due_job, delay_seconds=-5)  # already in the past — due now
    await schedule_retry(not_due_job, delay_seconds=300)  # 5 minutes out — not due

    assert await delayed_queue_depth() == 2
    assert await queue_depth() == 0

    moved = await promote_due_jobs()

    assert moved == 1
    assert await delayed_queue_depth() == 1  # not_due_job still waiting
    assert await queue_depth() == 1

    promoted = await dequeue_job(timeout=1)
    assert promoted["job_id"] == "due"


@pytest.mark.asyncio
async def test_promote_due_jobs_is_a_noop_when_nothing_is_due():
    job = {"job_id": "future", "product_id": 1, "url": "https://www.daraz.pk/a-i1-s1.html"}
    await schedule_retry(job, delay_seconds=300)

    moved = await promote_due_jobs()

    assert moved == 0
    assert await delayed_queue_depth() == 1
    assert await queue_depth() == 0


@pytest.mark.asyncio
async def test_delayed_queue_depth_accurate():
    assert await delayed_queue_depth() == 0
    await schedule_retry({"job_id": "a", "product_id": 1, "url": "x"}, delay_seconds=60)
    assert await delayed_queue_depth() == 1
