# The queue implementation lives in shared/queue.py because the API also
# needs it (POST /products/{id}/scrape enqueues, GET /queue/depth and the
# dead-letter endpoints read it). This module just re-exports it so worker
# code can `from app.queue import ...` per the project's file layout.
from shared.queue import (  # noqa: F401
    dead_letter,
    dead_letter_depth,
    dequeue_job,
    enqueue_job,
    get_dead_letter,
    list_dead_letters,
    promote_due_jobs,
    purge_dead_letter,
    queue_depth,
    replay_dead_letter,
    schedule_retry,
)
