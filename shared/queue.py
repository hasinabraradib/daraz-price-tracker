import json
from datetime import datetime, timezone

import redis.asyncio as redis

from shared.config import settings

_redis = redis.from_url(settings.redis_url, decode_responses=True)


async def enqueue_job(product_id: int, url: str, attempt: int = 1) -> None:
    job = {
        "product_id": product_id,
        "url": url,
        "attempt": attempt,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    await _redis.rpush(settings.queue_name, json.dumps(job))


async def dequeue_job(timeout: int = 5) -> dict | None:
    """Blocking pop with a timeout (seconds). Returns None if nothing arrived in time."""
    result = await _redis.blpop(settings.queue_name, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    return json.loads(raw)


async def queue_depth() -> int:
    return await _redis.llen(settings.queue_name)
