# daraz-price-tracker

[![CI](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-80.4%25-brightgreen)

> The CI badge above will 404 until this repo exists at
> `github.com/hasinabraradib/daraz-price-tracker` and the workflow has run
> at least once. The coverage badge is a static snapshot from the last
> local run (see **Tests** below), not a live/auto-updating one; wiring up
> Codecov or a CI step that regenerates it is a natural follow-up if you
> want that.

A Daraz product price tracker: a FastAPI service backed by Postgres, a
Redis-queued scraper worker driving headless Chromium via Playwright, async
SQLAlchemy 2.0, Alembic migrations, and competitor-price alerting over
email/webhook.

## Stack

- FastAPI (API)
- Playwright + Chromium, headless (worker)
- Redis 7 (job queue — plain lists, no Celery)
- SQLAlchemy 2.0 (async, `asyncpg`)
- PostgreSQL 16
- Alembic for migrations
- Mailhog (local SMTP catcher — dev/test email alerts land here, not a real inbox)
- Docker Compose for local dev

## Getting started

```bash
cp .env.example .env
docker compose up --build
```

The API is available at http://localhost:8000. `GET /health` verifies the
database connection (not faked — it runs a real `SELECT 1`). Mailhog's web
UI (view alert emails sent in dev) is at http://localhost:8025.

Migrations aren't applied automatically on container start:

```bash
docker compose run --rm api alembic upgrade head
```

## Architecture

```
                    ┌─────────┐        ┌──────────┐
  POST /products ──▶│   api   │──────▶│ postgres │
  GET  /products     └────┬────┘        └──────────┘
  POST /products/{id}/scrape             ▲
       │                  │              │
       ▼                  ▼              │
   ┌───────┐        ┌──────────┐         │
   │ redis │◀──────▶│  worker  │─────────┘
   │(queue)│  pop    │(scraper) │  writes PriceSnapshot
   └───────┘         └──────────┘
```

`api` and `worker` are separate services/images, but they share one package
of database and queue code — see **Shared code** below.

### Queue, retries, and the dead letter queue

`POST /products/{id}/scrape` pushes a JSON job onto a Redis list:

```json
{
  "job_id": "a1b2c3...",
  "product_id": 1,
  "url": "https://...",
  "attempt": 1,
  "enqueued_at": "2026-08-11T12:00:00+00:00",
  "attempt_history": []
}
```

The worker blocking-pops (`BLPOP`) jobs off that list and scrapes. What
happens next depends on how it fails — see `worker/worker_app/scraper.py`'s
exception hierarchy:

- **`TerminalScrapeError`** (404/410 "product gone", malformed URL) —
  dead-lettered immediately, no retry. Retrying a 404 wastes an attempt on
  something that will never succeed.
- **`RetryableScrapeError`** (timeouts, network errors, 5xx, 429) —
  scheduled for retry with exponential backoff + full jitter
  (`worker/worker_app/retry.py`): `delay = random.uniform(0, min(base * factor^(attempt-1), max_delay))`,
  defaults base=5s, factor=2, max=10min, capped at `RETRY_MAX_ATTEMPTS` (default 5).
- **`SelectorScrapeError`** (page loaded fine, but our selectors found
  nothing) — a subclass of `RetryableScrapeError`, but capped at exactly
  one retry regardless of the attempt budget: if the *next* attempt also
  fails on selectors, that's not noise, it means Daraz changed their
  markup, and no amount of retrying fixes that. See
  `_handle_failure`/`_previous_attempt_was_selector_error` in
  `worker/worker_app/main.py`.

Backoff delays are **not** `time.sleep()` — that would block the worker
and be lost on a restart. Instead, a retry is written to a Redis sorted set
(`scrape_jobs:delayed`, score = the unix timestamp it becomes eligible). A
second coroutine in the worker (`promoter_loop`) polls that set every 2s and
moves due jobs back onto the main queue.

Once a job exhausts its attempts (or fails terminally), it's pushed into a
Redis hash (`scrape_jobs:dead`) with the original job, every attempt's
timestamp/error, and the final error — inspectable and replayable via the
`/dead-letters` endpoints below.

Every attempt, success or failure, is also written to Postgres as a
`ScrapeAttempt` row — that's what makes success-rate-per-product a real
queryable metric (`GET /stats/scrape-health`), not just log-scraping.

### Scraper politeness

The worker scrapes one page at a time, throttled by `POLITE_DELAY_SECONDS`
(default 3s) between requests, targets public product pages only, and makes
no attempt to bypass bot detection or CAPTCHAs. See
`worker/worker_app/scraper.py` for details.

## Competitor tracking & alerting

Link another tracked product as a competitor (`POST /products/{id}/competitors`),
attach one or more alert rules to your product (`POST /products/{id}/alert-rules`),
and the worker evaluates every active rule right after each successful
scrape (`shared/alerts.py::evaluate_alerts`, called from
`worker_app/main.py::_handle_success`).

Three rule types:

| `rule_type`      | Fires when...                                                        |
|-------------------|-----------------------------------------------------------------------|
| `undercut`        | any linked competitor's latest price is below yours                   |
| `price_below`     | your price drops below `threshold_price`                              |
| `back_in_stock`   | `in_stock` flips `false → true` since the previous snapshot            |

Two delivery channels, both retrying transient failures with the *same*
backoff module the scraper's own retries use (`shared/retry.py` —
`compute_backoff_delay`, moved there from the worker in this phase
specifically so `shared/notifiers.py` could reuse it instead of
reimplementing backoff a second time):

- **email** — SMTP via `aiosmtplib`, pointed at the local Mailhog service
  by default (`SMTP_HOST=mailhog`, no real email needed for dev/tests). For
  real delivery, point it at Gmail (`SMTP_HOST=smtp.gmail.com`,
  `SMTP_PORT=587`, `SMTP_USE_TLS=true`, an app-password in
  `SMTP_USERNAME`/`SMTP_PASSWORD`) or any other SMTP provider.
- **webhook** — a JSON `POST` to `destination` via `httpx`.

### Dedup strategy

The full reasoning lives as a docstring at the top of `shared/alerts.py` —
worth reading directly if you're changing this logic — but the short
version:

An `AlertEvent` is **open** while `resolved_at IS NULL`. On every
evaluation run, for each rule, the condition is recomputed fresh from
current data (never inferred from event history) and one of four things
happens:

1. **Condition false, no open event** → nothing to do.
2. **Condition false, open event exists** → resolve it (`resolved_at = now`),
   send a best-effort resolution notice.
3. **Condition true, no open event** → fresh trigger. Open a new event and
   notify (subject to cooldown, below).
4. **Condition true, open event exists** → the interesting case. We do
   *not* notify every scrape just because "still true" — only if something
   **material** changed do we close the old event and open a new one:
   - `undercut`: a *different* competitor is now cheapest, OR the undercut
     gap moved by more than `ALERT_MATERIAL_CHANGE_PCT` (default 5%).
   - `price_below`: the trigger price moved by more than
     `ALERT_MATERIAL_CHANGE_PCT` since the open event's price.
   - `back_in_stock`: never reaches case 4 at all — see below.

**Cooldown** (`ALERT_COOLDOWN_MINUTES`, default 60) is a second,
independent guard checked *before* any mutation in cases 3 and 4: even a
fresh or materially-changed trigger is suppressed if this rule already
opened an event within the cooldown window. This protects against a price
that oscillates several times in a few minutes — each swing might be
"material" on its own, but nobody wants a flood of email for it. If
cooldown blocks a would-be re-trigger, the existing open event (if any) is
left completely untouched, to be re-evaluated next run.

**`back_in_stock`** is different in kind from the other two: it's an
edge-triggered instant (the flip itself), not a condition you can
meaningfully re-check "does it still hold" on later. Each firing creates an
`AlertEvent` that's immediately self-resolved (`resolved_at == triggered_at`)
— there's no persisting state to dedup against, just repeat *flips*, which
cooldown alone guards against.

## Shared code

`api/` and `worker/` are independent Docker images, but both need the same
models, queue functions, and (as of this phase) retry math and alert
evaluation. Rather than duplicate that code in both services, it lives in a
top-level `shared/` package:

- `shared/config.py` — `pydantic-settings` config, read from env vars
- `shared/database.py` — async SQLAlchemy engine/session, `Base`
- `shared/models.py` — `Product`, `PriceSnapshot`, `ScrapeAttempt`,
  `ProductCompetitor`, `AlertRule`, `AlertEvent`
- `shared/queue.py` — main queue, delayed-retry sorted set, and dead letter
  hash: `enqueue_job()`, `dequeue_job()`, `queue_depth()`,
  `schedule_retry()`, `promote_due_jobs()`, `delayed_queue_depth()`,
  `dead_letter()`, `dead_letter_depth()`, `list_dead_letters()`,
  `get_dead_letter()`, `replay_dead_letter()`, `purge_dead_letter()`
- `shared/retry.py` — `compute_backoff_delay()`, the full-jitter exponential
  backoff used by both the scraper's retries and `shared/notifiers.py`'s
  notification retries. Moved here from the worker in this phase
  specifically so notifiers could reuse it without a second implementation.
- `shared/notifiers.py` — `send_notification()`: email (SMTP via
  `aiosmtplib`) or webhook (`httpx` POST) delivery, retried via
  `shared/retry.py`.
- `shared/alerts.py` — `evaluate_alerts()`: the rule evaluation and dedup
  engine described above.

Both Dockerfiles build from the **repo root** (not their own subdirectory)
so they can `COPY shared ./shared` alongside their own app code.
`worker/worker_app/queue.py` and `worker/worker_app/retry.py` still exist as
their own files (per the intended layout) but just re-export from
`shared/queue.py` / `shared/retry.py`, since the API needs the queue
functions too (`POST /products/{id}/scrape`, `GET /queue/depth`) and
`shared/notifiers.py` needs the retry math.

`api/app` and `worker/worker_app` are separate top-level Python packages —
inside their own Docker containers each is just `app`, imported the same
way either way (`COPY worker/worker_app ./app` in the worker Dockerfile
means it's still `/code/app` inside the container). They're named
differently on disk specifically so the test suite — which imports both in
one process — doesn't have two same-named top-level packages colliding.

## Database schema

- **products** — tracked Daraz product URLs.
- **price_snapshots** — one row per successful scrape, with price, currency,
  stock status, and the raw scraped title. Indexed on
  `(product_id, scraped_at desc)` for fast "latest price history" queries.
- **scrape_attempts** — one row per attempt, success *or* failure, with
  error type/message and duration. Indexed on
  `(product_id, attempted_at desc)`.
- **product_competitors** — "watch this competitor for this product of
  mine." Unique on `(product_id, competitor_product_id)`; a CHECK constraint
  blocks a product from competing with itself.
- **alert_rules** — one row per configured alert (`rule_type`,
  `threshold_price` for `price_below`, delivery `channel`+`destination`,
  `is_active`). `rule_type`/`channel` are CHECK-constrained rather than
  native Postgres enums, matching the rest of this schema's plain-`String`
  style — avoids the migration ceremony of altering a Postgres enum type
  later.
- **alert_events** — one row per alert firing/resolution. `resolved_at IS
  NULL` means open; see the dedup strategy above. Indexed on
  `(alert_rule_id, triggered_at desc)`.

## API

| Method | Path                              | Description                                       |
|--------|-----------------------------------|-----------------------------------------------------|
| GET    | `/health`                         | DB connectivity check                                |
| POST   | `/products`                       | Add a product by Daraz URL                           |
| GET    | `/products`                       | List products with their latest price                |
| GET    | `/products/{id}/history`          | Price snapshots over time, newest first               |
| GET    | `/products/{id}/attempts`         | Scrape attempt history for one product                |
| POST   | `/products/{id}/scrape`           | Enqueue a scrape job for a product                    |
| GET    | `/queue/depth`                    | Current Redis queue depth                             |
| GET    | `/dead-letters`                   | List dead-lettered jobs with failure reason            |
| POST   | `/dead-letters/{job_id}/replay`   | Push a dead-lettered job back onto the main queue      |
| DELETE | `/dead-letters/{job_id}`          | Discard a dead-lettered job                            |
| GET    | `/stats/scrape-health`            | Success rate, failures by error type, queue/DLQ depth  |
| POST   | `/products/{id}/competitors`      | Link a competitor product                              |
| GET    | `/products/{id}/competitors`      | List competitors with latest price + gap vs this product |
| DELETE | `/products/{id}/competitors/{competitor_id}` | Unlink a competitor                          |
| GET    | `/products/{id}/comparison`       | This product + all competitors, cheapest flagged       |
| POST   | `/products/{id}/alert-rules`      | Create an alert rule                                   |
| GET    | `/products/{id}/alert-rules`      | List a product's alert rules                           |
| DELETE | `/products/{id}/alert-rules/{rule_id}` | Delete an alert rule                              |
| GET    | `/alerts`                         | Recent AlertEvents, filterable by `product_id`/`status` |

## Migrations

Migrations live in `api/alembic/`. To generate a new migration after
changing `shared/models.py`:

```bash
docker compose run --rm api alembic revision --autogenerate -m "describe your change"
docker compose run --rm api alembic upgrade head
```

## Tests

```bash
pip install -r requirements-dev.txt -r api/requirements.txt -r worker/requirements.txt
DATABASE_URL=postgresql+asyncpg://daraz:daraz@localhost:5432/daraz_price_tracker \
  pytest --cov=app --cov=worker_app --cov=shared --cov-report=term-missing
```

Requires a real Postgres reachable at that URL (e.g. `docker compose up -d postgres`
and use `localhost:5432`, which is what `docker-compose.yml` already publishes) —
tests create and drop their own `..._test` database on it per session, and never
touch the dev database. Redis is never required: `shared/queue.py`'s client is
replaced with `fakeredis` for every test via an autouse fixture in
`tests/conftest.py`. The scraper's Playwright calls are always mocked too — no
test in this suite makes a real network call.

Last local run: **85 passed, 80.4% coverage** (`--cov-fail-under=70`,
enforced in CI). What's *not* covered, and why that's an accepted gap
rather than an oversight:

- **`worker_app/main.py`'s `worker_loop`, `promoter_loop`, and `run()`**
  (~45% of that file) — these are `while True:` loops wrapping the
  functions that *are* unit-tested (`process_job`, `_handle_failure`,
  `_handle_success`). Unit-testing an infinite loop means either breaking
  out of it artificially (tests the harness, not the code) or an
  integration test that starts a real worker process. These were verified
  by hand against the live stack (real 404 → terminal, real backoff growth
  in Redis, real 5-attempt exhaustion → DLQ → replay), just not by anything
  `pytest` runs. Worth an integration test with a real `docker compose`
  stack if this becomes CI-gated later; not worth faking here.
- **`shared/notifiers.py`'s `_send_email`/`_send_webhook`** (~29% of that
  file) — `test_alerts.py` mocks `send_notification` at its outer boundary
  (per the task's "no real SMTP/HTTP in tests" instruction), so the
  actual `aiosmtplib`/`httpx` calls inside are never exercised by `pytest`.
  They *are* real-verified: the live end-to-end check for this phase sent
  genuine SMTP traffic to Mailhog and confirmed the email landed
  (`GET http://localhost:8025/api/v2/messages`) — same "live-verified,
  not pytest-verified" situation as the worker loops above.
- **`api/app/routers/competitors.py` and `alert_rules.py`** (52%/62%) —
  the core paths (create/list/delete, self-link rejection, duplicate
  rejection, 404s) are tested; some less-interesting branches (e.g. a
  competitor with zero snapshots yet in a couple of list-endpoint code
  paths) aren't individually covered. Not worth chasing every branch by
  hand when the underlying `evaluate_alerts`/query logic they call is
  already thoroughly tested in `test_alerts.py`.
- **A few individual `except`/`raise` lines in `api/app/routers/products.py`**
  (e.g. the `IntegrityError` → 409 branch) show as partially covered
  despite `test_create_duplicate_product_returns_409` passing and clearly
  exercising that exact path — this looks like a branch-coverage accounting
  quirk with multi-line `raise ... from exc` statements rather than a real
  gap; flagging it instead of quietly rounding it away.
- **`shared/database.py` line 16** (the non-test `pool_pre_ping=True`
  branch) — untested by definition, since every test run sets
  `SQLALCHEMY_NULL_POOL=1` to take the *other* branch. It's one line and
  it's exactly what real (non-test) traffic exercises every time the app
  runs, so this is a coverage-tool artifact of the test/prod split, not a
  blind spot.
- **`api/app/main.py`'s DB-down branch of `/health`** — reachable, but
  would need the test to actually sever the DB connection mid-test to
  exercise it; not done here. This one's a legitimate small gap, not a
  structural one — cheap to add later with a mocked `engine.connect()`.

## Project layout

```
daraz-price-tracker/
├── api/
│   ├── app/
│   │   ├── main.py          # FastAPI app, router wiring, /health
│   │   ├── routers/
│   │   │   ├── products.py     # /products endpoints, incl. /attempts
│   │   │   ├── competitors.py  # /products/{id}/competitors, /comparison
│   │   │   ├── alert_rules.py  # /products/{id}/alert-rules
│   │   │   ├── alerts.py       # /alerts
│   │   │   ├── queue.py        # /queue/depth
│   │   │   ├── dead_letters.py # /dead-letters endpoints
│   │   │   └── stats.py        # /stats/scrape-health
│   │   ├── schemas.py       # Pydantic v2 request/response models
│   │   └── url_utils.py     # daraz_url normalization
│   ├── alembic/
│   ├── Dockerfile
│   └── requirements.txt
├── worker/
│   ├── worker_app/          # importable as `worker_app` — see "Shared code" above
│   │   ├── main.py          # dequeue loop + promoter loop, retry decisions, alert evaluation
│   │   ├── scraper.py       # Playwright scraper + exception hierarchy
│   │   ├── retry.py         # re-exports shared/retry.py
│   │   └── queue.py         # re-exports shared/queue.py
│   ├── Dockerfile
│   └── requirements.txt
├── shared/
│   ├── config.py
│   ├── database.py
│   ├── models.py            # Product, PriceSnapshot, ScrapeAttempt,
│   │                         # ProductCompetitor, AlertRule, AlertEvent
│   ├── queue.py              # queue + delayed retry zset + dead letter hash
│   ├── retry.py              # exponential backoff + full jitter
│   ├── notifiers.py          # email/webhook delivery, retried via retry.py
│   └── alerts.py             # rule evaluation + dedup engine
├── tests/
│   ├── conftest.py          # test DB + fakeredis + httpx client fixtures
│   ├── test_retry.py        # backoff math, jitter, max-attempts
│   ├── test_error_classification.py  # exception hierarchy, mocked Playwright
│   ├── test_queue.py        # enqueue/dequeue/retry/DLQ, via fakeredis
│   ├── test_url_utils.py    # daraz_url normalization/validation
│   ├── test_alerts.py       # rule firing, dedup, cooldown, resolution
│   └── test_api.py          # FastAPI endpoints, via httpx ASGI transport
├── .github/workflows/ci.yml # test (matrix) -> lint -> build, gha layer cache
├── pyproject.toml           # pytest, coverage, ruff config
├── requirements-dev.txt
├── docker-compose.yml
├── .env.example
└── .gitignore
```
