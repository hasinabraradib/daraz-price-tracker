# daraz-price-tracker

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

### Queue

`POST /products/{id}/scrape` (or nothing yet, since nothing schedules jobs
on its own in this phase) pushes a JSON job onto a Redis list:

```json
{"product_id": 1, "url": "https://...", "attempt": 1, "enqueued_at": "2026-08-11T12:00:00+00:00"}
```

The worker blocking-pops (`BLPOP`) jobs off that list, scrapes the page, and
writes a `PriceSnapshot` row. Failures are logged as structured JSON and
dropped — retry logic is a later phase, not implemented here.

### Scraper politeness

The worker scrapes one page at a time, throttled by `POLITE_DELAY_SECONDS`
(default 3s) between requests, targets public product pages only, and makes
no attempt to bypass bot detection or CAPTCHAs. See
`worker/app/scraper.py` for details.

## Shared code

`api/` and `worker/` are independent Docker images, but both need the same
`Product`/`PriceSnapshot` models and the same Redis queue functions (the API
enqueues jobs and reports queue depth; the worker dequeues and writes
snapshots). Rather than duplicate that code in both services, it lives in a
top-level `shared/` package:

- `shared/config.py` — `pydantic-settings` config, read from env vars
- `shared/database.py` — async SQLAlchemy engine/session, `Base`
- `shared/models.py` — `Product`, `PriceSnapshot`
- `shared/queue.py` — `enqueue_job()`, `dequeue_job()`, `queue_depth()`

Both Dockerfiles build from the **repo root** (not their own subdirectory)
so they can `COPY shared ./shared` alongside their own app code.
`worker/app/queue.py` still exists as its own file (per the intended layout)
but just re-exports from `shared/queue.py`, since the API needs those same
functions for `POST /products/{id}/scrape` and `GET /queue/depth`.

## Database schema

- **products** — tracked Daraz product URLs.
- **price_snapshots** — one row per scrape, with price, currency, stock
  status, and the raw scraped title. Indexed on `(product_id, scraped_at desc)`
  for fast "latest price history" queries.

## API

| Method | Path                        | Description                                  |
|--------|-----------------------------|-----------------------------------------------|
| GET    | `/health`                   | DB connectivity check                          |
| POST   | `/products`                 | Add a product by Daraz URL                     |
| GET    | `/products`                 | List products with their latest price          |
| GET    | `/products/{id}/history`    | Price snapshots over time, newest first        |
| POST   | `/products/{id}/scrape`     | Enqueue a scrape job for a product              |
| GET    | `/queue/depth`               | Current Redis queue depth                       |

## Migrations

Migrations live in `api/alembic/`. To generate a new migration after
changing `shared/models.py`:

```bash
docker compose run --rm api alembic revision --autogenerate -m "describe your change"
docker compose run --rm api alembic upgrade head
```

## Project layout

```
daraz-price-tracker/
├── api/
│   ├── app/
│   │   ├── main.py          # FastAPI app, router wiring, /health
│   │   ├── routers/
│   │   │   ├── products.py  # /products endpoints
│   │   │   └── queue.py     # /queue/depth
│   │   └── schemas.py       # Pydantic v2 request/response models
│   ├── alembic/
│   ├── Dockerfile
│   └── requirements.txt
├── worker/
│   ├── app/
│   │   ├── main.py          # blocking-pop loop, writes PriceSnapshot
│   │   ├── scraper.py       # Playwright-based Daraz scraper
│   │   └── queue.py         # re-exports shared/queue.py
│   ├── Dockerfile
│   └── requirements.txt
├── shared/
│   ├── config.py
│   ├── database.py
│   ├── models.py            # Product, PriceSnapshot
│   └── queue.py             # Redis queue mechanics
├── docker-compose.yml
├── .env.example
└── .gitignore
```
