# daraz-price-tracker

[![CI](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-83.5%25-brightgreen)

> The CI badge above will 404 until this repo exists at
> `github.com/hasinabraradib/daraz-price-tracker` and the workflow has run
> at least once. The coverage badge is a static snapshot from the last
> local run (see **Tests** below), not a live/auto-updating one; wiring up
> Codecov or a CI step that regenerates it is a natural follow-up if you
> want that.

A Daraz product price tracker: a FastAPI service backed by Postgres, a
Redis-queued scraper worker driving headless Chromium via Playwright, async
SQLAlchemy 2.0, and Alembic migrations.

## Stack

- FastAPI (API)
- Playwright + Chromium, headless (worker)
- Redis 7 (job queue — plain lists, no Celery)
- SQLAlchemy 2.0 (async, `asyncpg`)
- PostgreSQL 16
- Alembic for migrations
- Docker Compose for local dev

## Getting started

```bash
cp .env.example .env
docker compose up --build
```

The API is available at http://localhost:8000. `GET /health` verifies the
database connection (not faked — it runs a real `SELECT 1`).

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

## Shared code

`api/` and `worker/` are independent Docker images, but both need the same
`Product`/`PriceSnapshot` models and the same Redis queue functions (the API
enqueues jobs and reports queue depth; the worker dequeues and writes
snapshots). Rather than duplicate that code in both services, it lives in a
top-level `shared/` package:

- `shared/config.py` — `pydantic-settings` config, read from env vars
- `shared/database.py` — async SQLAlchemy engine/session, `Base`
- `shared/models.py` — `Product`, `PriceSnapshot`, `ScrapeAttempt`
- `shared/queue.py` — main queue, delayed-retry sorted set, and dead letter
  hash: `enqueue_job()`, `dequeue_job()`, `queue_depth()`,
  `schedule_retry()`, `promote_due_jobs()`, `delayed_queue_depth()`,
  `dead_letter()`, `dead_letter_depth()`, `list_dead_letters()`,
  `get_dead_letter()`, `replay_dead_letter()`, `purge_dead_letter()`

Both Dockerfiles build from the **repo root** (not their own subdirectory)
so they can `COPY shared ./shared` alongside their own app code.
`worker/worker_app/queue.py` still exists as its own file (per the intended layout)
but just re-exports from `shared/queue.py`, since the API needs those same
functions for `POST /products/{id}/scrape` and `GET /queue/depth`.

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

## API

| Method | Path                          | Description                                  |
|--------|-------------------------------|-----------------------------------------------|
| GET    | `/health`                     | DB connectivity check                          |
| POST   | `/products`                   | Add a product by Daraz URL                     |
| GET    | `/products`                   | List products with their latest price          |
| GET    | `/products/{id}/history`      | Price snapshots over time, newest first        |
| GET    | `/products/{id}/attempts`     | Scrape attempt history for one product         |
| POST   | `/products/{id}/scrape`       | Enqueue a scrape job for a product              |
| GET    | `/queue/depth`                | Current Redis queue depth                       |
| GET    | `/dead-letters`                | List dead-lettered jobs with failure reason     |
| POST   | `/dead-letters/{job_id}/replay`| Push a dead-lettered job back onto the main queue |
| DELETE | `/dead-letters/{job_id}`       | Discard a dead-lettered job                     |
| GET    | `/stats/scrape-health`         | Success rate, failures by error type, queue/DLQ depth |

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

Last local run: **59 passed, 83.5% coverage** (`--cov-fail-under=70`, enforced
in CI). What's *not* covered, and why that's an accepted gap rather than an
oversight:

- **`worker_app/main.py`'s `worker_loop`, `promoter_loop`, and `run()`**
  (~45% of that file) — these are `while True:` loops wrapping the
  functions that *are* unit-tested (`process_job`, `_handle_failure`,
  `_handle_success`). Unit-testing an infinite loop means either breaking
  out of it artificially (tests the harness, not the code) or an
  integration test that starts a real worker process. These were verified
  by hand against the live stack (real 404 → terminal, real backoff growth
  in Redis, real 5-attempt exhaustion → DLQ → replay — see the worker/DLQ
  phase of this project's history), just not by anything `pytest` runs.
  Worth an integration test with a real `docker compose` stack if this
  becomes CI-gated later; not worth faking here.
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
│   │   ├── main.py          # dequeue loop + promoter loop, retry decisions
│   │   ├── scraper.py       # Playwright scraper + exception hierarchy
│   │   ├── retry.py         # exponential backoff + full jitter
│   │   └── queue.py         # re-exports shared/queue.py
│   ├── Dockerfile
│   └── requirements.txt
├── shared/
│   ├── config.py
│   ├── database.py
│   ├── models.py            # Product, PriceSnapshot, ScrapeAttempt
│   └── queue.py             # queue + delayed retry zset + dead letter hash
├── tests/
│   ├── conftest.py          # test DB + fakeredis + httpx client fixtures
│   ├── test_retry.py        # backoff math, jitter, max-attempts
│   ├── test_error_classification.py  # exception hierarchy, mocked Playwright
│   ├── test_queue.py        # enqueue/dequeue/retry/DLQ, via fakeredis
│   ├── test_url_utils.py    # daraz_url normalization/validation
│   └── test_api.py          # FastAPI endpoints, via httpx ASGI transport
├── .github/workflows/ci.yml # test (matrix) -> lint -> build, gha layer cache
├── pyproject.toml           # pytest, coverage, ruff config
├── requirements-dev.txt
├── docker-compose.yml
├── .env.example
└── .gitignore
```
