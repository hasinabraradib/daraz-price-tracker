# daraz-price-tracker

Foundation for a Daraz product price tracker: a FastAPI service backed by
Postgres, with async SQLAlchemy 2.0 and Alembic migrations. The scraper
itself is not implemented yet — this is just the API + DB scaffolding.

## Stack

- FastAPI
- SQLAlchemy 2.0 (async, `asyncpg`)
- PostgreSQL 16
- Alembic for migrations
- Docker Compose for local dev

## Getting started

```bash
cp .env.example .env
docker compose up --build
```

The API will be available at http://localhost:8000, with a health check at
[http://localhost:8000/health](http://localhost:8000/health) that verifies
the database connection.

## Database schema

- **products** — tracked Daraz product URLs.
- **price_snapshots** — one row per scrape, with price, currency, stock
  status, and the raw scraped title. Indexed on `(product_id, scraped_at desc)`
  for fast "latest price history" queries.

## Migrations

Migrations live in `api/alembic/`. To generate a new migration after
changing `api/app/models.py`:

```bash
docker compose run --rm api alembic revision --autogenerate -m "describe your change"
docker compose run --rm api alembic upgrade head
```

The initial migration is already included in `api/alembic/versions/`. It is
not applied automatically on container start — run the `upgrade head`
command above after bringing the stack up.

## Project layout

```
daraz-price-tracker/
├── api/
│   ├── app/
│   │   ├── main.py       # FastAPI app, /health endpoint
│   │   ├── config.py     # pydantic-settings, reads env vars
│   │   ├── database.py   # async SQLAlchemy engine/session
│   │   ├── models.py     # Product, PriceSnapshot
│   │   └── schemas.py    # Pydantic v2 request/response models
│   ├── alembic/
│   ├── Dockerfile
│   └── requirements.txt
├── docker-compose.yml
├── .env.example
└── .gitignore
```
