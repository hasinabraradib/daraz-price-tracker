import json
import time
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis

from shared.config import settings

_redis = redis.from_url(settings.redis_url, decode_responses=True)

# Jobs move through three keys as they retry:
#   scrape_jobs           (list)  — ready to be picked up now
#   scrape_jobs:delayed   (zset)  — waiting out a backoff, score = due-at unix ts
#   scrape_jobs:dead      (hash)  — exhausted/terminal, keyed by job_id
DELAYED_QUEUE_KEY = f"{settings.queue_name}:delayed"
DEAD_LETTER_KEY = f"{settings.queue_name}:dead"


async def enqueue_job(product_id: int, url: str, attempt: int = 1) -> str:
    # Deferred import: shared.metrics imports queue_depth/etc. from this
    # module for gauge refresh, so importing shared.metrics back at module
    # level here would be circular. By the time this function actually
    # runs, both modules are fully loaded, so the import is safe.
    from shared.metrics import JOBS_ENQUEUED_TOTAL

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "product_id": product_id,
        "url": url,
        "attempt": attempt,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "attempt_history": [],
    }
    await _redis.rpush(settings.queue_name, json.dumps(job))
    JOBS_ENQUEUED_TOTAL.inc()
    return job_id


async def dequeue_job(timeout: int = 5) -> dict | None:
    """Blocking pop with a timeout (seconds). Returns None if nothing arrived in time."""
    result = await _redis.blpop(settings.queue_name, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    return json.loads(raw)


async def queue_depth() -> int:
    return await _redis.llen(settings.queue_name)


async def schedule_retry(job: dict, delay_seconds: float) -> None:
    """Park a job in the delayed set until its backoff elapses. Persisted in
    Redis (not an in-process sleep) so a worker restart doesn't lose it and
    doesn't block processing of other jobs while it waits."""
    due_at = time.time() + delay_seconds
    await _redis.zadd(DELAYED_QUEUE_KEY, {json.dumps(job): due_at})


async def promote_due_jobs(limit: int = 100) -> int:
    """Move jobs whose backoff has elapsed from the delayed set back onto
    the main queue. Each move is removal-then-push, and only jobs whose
    ZREM actually removed something get pushed — guards against
    double-delivery if this were ever called concurrently."""
    now = time.time()
    due = await _redis.zrangebyscore(DELAYED_QUEUE_KEY, min=0, max=now, start=0, num=limit)
    moved = 0
    for raw in due:
        removed = await _redis.zrem(DELAYED_QUEUE_KEY, raw)
        if removed:
            await _redis.rpush(settings.queue_name, raw)
            moved += 1
    return moved


async def delayed_queue_depth() -> int:
    return await _redis.zcard(DELAYED_QUEUE_KEY)


async def delayed_jobs_with_due_at() -> list[tuple[dict, float]]:
    """For inspection/tests: every delayed job paired with its due-at unix timestamp."""
    raw = await _redis.zrange(DELAYED_QUEUE_KEY, 0, -1, withscores=True)
    return [(json.loads(member), score) for member, score in raw]


async def dead_letter(job: dict, final_error_type: str, final_error_message: str) -> str:
    job_id = job["job_id"]
    record = {
        "job_id": job_id,
        "original_job": {
            "product_id": job["product_id"],
            "url": job["url"],
            "enqueued_at": job["enqueued_at"],
        },
        "attempts": job.get("attempt_history", []),
        "final_error_type": final_error_type,
        "final_error_message": final_error_message,
        "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
    }
    await _redis.hset(DEAD_LETTER_KEY, job_id, json.dumps(record))
    return job_id


async def dead_letter_depth() -> int:
    return await _redis.hlen(DEAD_LETTER_KEY)


async def list_dead_letters() -> list[dict]:
    raw = await _redis.hgetall(DEAD_LETTER_KEY)
    return [json.loads(v) for v in raw.values()]


async def get_dead_letter(job_id: str) -> dict | None:
    raw = await _redis.hget(DEAD_LETTER_KEY, job_id)
    return json.loads(raw) if raw else None


async def replay_dead_letter(job_id: str) -> bool:
    """Push a dead-lettered job back onto the main queue as a fresh job
    (attempt reset to 1, history cleared) and remove it from the DLQ."""
    raw = await _redis.hget(DEAD_LETTER_KEY, job_id)
    if raw is None:
        return False
    record = json.loads(raw)
    original = record["original_job"]
    job = {
        "job_id": record["job_id"],
        "product_id": original["product_id"],
        "url": original["url"],
        "attempt": 1,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "attempt_history": [],
    }
    await _redis.rpush(settings.queue_name, json.dumps(job))
    await _redis.hdel(DEAD_LETTER_KEY, job_id)
    return True


async def purge_dead_letter(job_id: str) -> bool:
    removed = await _redis.hdel(DEAD_LETTER_KEY, job_id)
    return bool(removed)
