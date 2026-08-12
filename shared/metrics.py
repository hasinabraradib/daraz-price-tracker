"""Prometheus metrics, defined once and imported by both services so `api`
and `worker` report under the same metric names/labels rather than two
independent (and inevitably drifting) definitions.

Counters and histograms are updated inline, at the moment the thing they
measure actually happens — see worker_app/main.py (scrapes), shared/alerts.py
(alerts firing), shared/notifiers.py (deliveries), shared/queue.py (enqueue).
That's the easy, standard part.

Gauges are different in kind, and worth being deliberate about. A counter
is something *we* accumulate as events happen in-process — incrementing it
at the event site is correct by construction. A gauge here represents
state that already lives somewhere else entirely (a Redis list/zset/hash
length, a Postgres row count) — the queue and the products table are the
source of truth, not this process. Setting a gauge "on write" (e.g. +1 in
enqueue_job, -1 in dequeue_job) would mean re-deriving that external state
from scratch by tracking every mutation, across however many worker/API
processes are running, and one missed decrement (a crash mid-job, a
manual `redis-cli` poke, a replayed dead letter) permanently desyncs the
number from reality with no way to self-heal. Instead we just ask Redis
and Postgres what's true, fresh, every time metrics are scraped — see
`refresh_gauges()` below. That function is:

  - awaited directly inside the API's `GET /metrics` handler (an async
    request handler already running in the event loop), so the API's
    gauges are refreshed at *exactly* scrape time, every time.
  - run on a timer by the worker instead (`worker_app/main.py`'s
    `metrics_refresh_loop`), because `prometheus_client.start_http_server`
    runs a plain background thread with no per-request hook to await
    anything from — there's nowhere to plug an async refresh into a
    request. The worker's gauges are therefore "refreshed every
    METRICS_REFRESH_INTERVAL_SECONDS" rather than "refreshed at the
    instant of the scrape" — a small, deliberate, documented gap (a real
    Prometheus server polls every 15-30s anyway, so a 5s-old gauge value
    is not meaningfully different from a fresh one), not an oversight.

We considered a genuine custom `Collector` (registered so `collect()` runs
automatically on every scrape, no manual refresh call needed at all) — the
textbook-correct pattern for this. We didn't use it because `collect()` is
a synchronous callback in prometheus_client, and every query it would need
(Redis via redis.asyncio, Postgres via an async SQLAlchemy session) is
async in this codebase; bridging that would mean either a second set of
sync clients/drivers just for metrics, or nesting an event loop inside
one that may already be running (breaks inside FastAPI's request handler
specifically). Plain async Gauges refreshed from an async context, as
below, gets the same "not stale" property without either problem.
"""
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select

from shared.database import async_session_factory
from shared.models import Product
from shared.queue import dead_letter_depth, delayed_queue_depth, queue_depth

# ---- counters ----

SCRAPE_ATTEMPTS_TOTAL = Counter(
    "scrape_attempts_total",
    "Scrape attempts, by outcome",
    ["outcome", "error_type"],
)

ALERTS_FIRED_TOTAL = Counter(
    "alerts_fired_total",
    "AlertEvents opened — fresh triggers and material re-triggers, not resolutions",
    ["rule_type", "channel"],
)

ALERT_DELIVERIES_TOTAL = Counter(
    "alert_deliveries_total",
    "Notification delivery attempts, by channel and outcome",
    ["channel", "status"],
)

JOBS_ENQUEUED_TOTAL = Counter(
    "jobs_enqueued_total",
    "Scrape jobs pushed onto the main queue",
)

JOBS_DEAD_LETTERED_TOTAL = Counter(
    "jobs_dead_lettered_total",
    "Jobs moved to the dead letter queue, by reason",
    ["reason"],
)

# ---- gauges (point-in-time external state; see refresh_gauges() above) ----

QUEUE_DEPTH = Gauge("queue_depth", "Current main scrape queue length")
DELAYED_QUEUE_DEPTH = Gauge("delayed_queue_depth", "Jobs currently waiting out a retry backoff")
DEAD_LETTER_DEPTH = Gauge("dead_letter_depth", "Jobs currently in the dead letter queue")
PRODUCTS_TRACKED_TOTAL = Gauge(
    "products_tracked_total", "Tracked products, by active status", ["is_active"]
)

# ---- histograms ----

SCRAPE_DURATION_SECONDS = Histogram(
    "scrape_duration_seconds",
    "Time to scrape a single product page (one attempt)",
    # Playwright launching a real browser is slow — seconds, not
    # milliseconds — so the default 5ms-10s buckets would bucket
    # almost everything into the top overflow bin. Tuned for 1-30s.
    buckets=(1, 2, 3, 5, 8, 13, 20, 30),
)

ALERT_DELIVERY_DURATION_SECONDS = Histogram(
    "alert_delivery_duration_seconds",
    "Time to deliver a single alert notification, including retries",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)


async def refresh_gauges() -> None:
    """Query current state from Redis and Postgres and update the gauges.
    Call this immediately before rendering /metrics — see module docstring
    for why gauges need this and counters don't."""
    QUEUE_DEPTH.set(await queue_depth())
    DELAYED_QUEUE_DEPTH.set(await delayed_queue_depth())
    DEAD_LETTER_DEPTH.set(await dead_letter_depth())

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Product.is_active, func.count()).group_by(Product.is_active)
            )
        ).all()
    counts = dict(rows)
    PRODUCTS_TRACKED_TOTAL.labels(is_active="true").set(counts.get(True, 0))
    PRODUCTS_TRACKED_TOTAL.labels(is_active="false").set(counts.get(False, 0))
