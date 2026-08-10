# The queue implementation lives in shared/queue.py because the API also
# needs it (POST /products/{id}/scrape enqueues, GET /queue/depth reads it).
# This module just re-exports it so worker code can `from app.queue import ...`
# per the project's file layout.
from shared.queue import dequeue_job, enqueue_job, queue_depth  # noqa: F401
