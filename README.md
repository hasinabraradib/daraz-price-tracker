# daraz-price-tracker

[![CI](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-84.4%25-brightgreen)

> The CI badge above will 404 until this repo exists at
> `github.com/hasinabraradib/daraz-price-tracker` and the workflow has run
> at least once. The coverage badge is a static snapshot from the last
> local run (see **Tests** below), not a live/auto-updating one; wiring up
> Codecov or a CI step that regenerates it is a natural follow-up if you
> want that.

A Daraz product price tracker: a FastAPI service backed by Postgres, a
Redis-queued scraper worker driving headless Chromium via Playwright, async
SQLAlchemy 2.0, Alembic migrations, and price/stock alerting over
email, Discord, or generic webhook — for sellers watching competitors and
for shoppers watching a price.

## Stack

- FastAPI (API)
- Playwright + Chromium, headless (worker)
- Redis 7 (job queue — plain lists, no Celery)
- SQLAlchemy 2.0 (async, `asyncpg`)
- PostgreSQL 16
- Alembic for migrations
- Mailhog (local SMTP catcher — dev/test email alerts land here, not a real inbox)
- Discord webhooks (auto-detected — see **Alert delivery** below)
- Prometheus client metrics (`GET /metrics` on the API, `:9100/metrics` on
  the worker — see **Metrics** below), scraped by a Prometheus server and
  visualized in Grafana when deployed to Kubernetes (see **Monitoring**
  under **Kubernetes** below) — docker-compose alone doesn't run either
- Docker Compose for local dev, Kubernetes manifests for a local minikube
  deploy (see **Kubernetes** below)

## Getting started

```bash
cp .env.example .env
docker compose up --build
```

The API is available at http://localhost:8000. `GET /health` verifies the
database connection (not faked — it runs a real `SELECT 1`). Mailhog's web
UI (view alert emails sent in dev) is at http://localhost:8025. Metrics:
`curl localhost:8000/metrics` (API) and `curl localhost:9100/metrics` (worker).

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
`worker_app/main.py::_handle_success`). The same system serves two
audiences: `undercut` is for sellers watching competitors; `price_below`,
`price_drop_pct`, and `back_in_stock` are for shoppers watching a price.

Four rule types, two different *shapes* of condition:

| `rule_type`      | Fires when...                                                          | Shape        |
|-------------------|--------------------------------------------------------------------------|--------------|
| `undercut`        | any linked competitor's latest price is below yours                      | persisting   |
| `price_below`     | your price drops below `threshold_price`                                 | persisting   |
| `price_drop_pct`  | price drops by more than `threshold_pct` vs. the *immediately prior* snapshot | edge-triggered |
| `back_in_stock`   | `in_stock` flips `false → true` since the previous snapshot               | persisting   |

**Persisting** conditions can stay true across many scrapes and use the
open/resolve dedup dance below. **Edge-triggered** (`price_drop_pct`) is
only ever "true" on the exact scrape a qualifying drop happens — it can't
meaningfully be "still true" next scrape unless a brand new drop occurs
then too, so it self-resolves immediately (`resolved_at == triggered_at`)
and skips the open/resolve dance; cooldown alone guards against repeats.

### Alert delivery

Three delivery paths, all going through `shared/notifiers.py`, which
retries transient failures with the *same* backoff module the scraper's
own retries use (`shared/retry.py` — `compute_backoff_delay`, moved there
specifically so notifiers could reuse it instead of reimplementing
backoff):

- **email** (`channel: "email"`) — SMTP via `aiosmtplib`, pointed at the
  local Mailhog service by default (`SMTP_HOST=mailhog`, no real email
  needed for dev/tests). For real delivery, point it at Gmail
  (`SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USE_TLS=true`, an
  app-password in `SMTP_USERNAME`/`SMTP_PASSWORD`) or any other SMTP
  provider.
- **webhook** (`channel: "webhook"`) — a JSON `POST` to `destination` via
  `httpx`. The full structured payload (`rule_type`, `product_name`,
  `product_url`, `trigger_price`, `competitor_price`, ...) goes out as-is —
  suited to something that parses JSON, e.g. your own service.
- **Discord webhook** — also `channel: "webhook"`, `destination` set to
  your Discord webhook URL. `shared/notifiers.py` detects URLs containing
  `discord.com/api/webhooks` automatically (no separate channel value
  needed) and POSTs Discord's required `{"content": "..."}` shape instead
  of the generic payload. The `content` string is the *same*
  human-readable message used for the email body — product name, what
  triggered, price change, and a link to the product page — built once in
  `shared/alerts.py::_deliver`, not reimplemented per channel.

  To try it: paste your real webhook URL into `DISCORD_WEBHOOK_URL` in
  your own `.env` (never commit a real value — `.env.example` only has a
  placeholder), then create an `AlertRule` with `channel: "webhook"` and
  that URL as `destination`.

### Dedup strategy

The full reasoning lives as a docstring at the top of `shared/alerts.py` —
worth reading directly if you're changing this logic — but the short
version, for the three **persisting** rule types:

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
   - `price_below`: the price moved more than `ALERT_MATERIAL_CHANGE_PCT`
     **further below the threshold** since the open event — measured as
     percent change in `(threshold − price)`, not in the raw price. A
     threshold of 1000 going 990 → 980 is a tiny ~1% raw move, but the gap
     below threshold *doubles* (10 → 20) — material, and correctly so:
     "further below" is what the user actually cares about, not the raw
     percentage.
   - `back_in_stock`: never fires again here while continuously in stock
     — see below, this is what stops it from re-notifying every scrape.

**Cooldown** (`ALERT_COOLDOWN_MINUTES`, default 60) is a second,
independent guard checked *before* any mutation in cases 3 and 4: even a
fresh or materially-changed trigger is suppressed if this rule already
opened an event within the cooldown window. This protects against a price
that oscillates several times in a few minutes — each swing might be
"material" on its own, but nobody wants a flood of email for it. If
cooldown blocks a would-be re-trigger, the existing open event (if any) is
left completely untouched, to be re-evaluated next run.

**`back_in_stock`** fits the same four-case shape as `undercut`/`price_below`
— unlike an earlier version of this rule, which fired once and immediately
self-resolved. Now "holds" tracks whether the product is *currently* in
stock, and the open event represents "there is a live in-stock streak the
user has been told about." It resolves the moment the product goes out of
stock again (case 2) — so the *next* restock after that is a genuinely
fresh `AlertEvent` (case 3), not a reopen of the old one. The one wrinkle:
a *fresh* trigger (case 3, no open event) only fires on a real observed
flip (previous snapshot recorded out-of-stock, latest in-stock) —
otherwise the very first snapshot ever taken while in stock would look
like a "restock" with nothing to compare against.

**`price_drop_pct`** doesn't participate in any of this — it's
edge-triggered (see above). "A NEW drop occurred that is itself larger
than the threshold — not just the price staying low" falls out for free:
the condition is defined entirely in terms of the two most recent
snapshots, so it can only be true again if another qualifying drop
actually happens; the price merely *staying* low changes nothing, because
"previous" has moved on to a newer snapshot each time.

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
  `threshold_price` for `price_below`, `threshold_pct` for `price_drop_pct`,
  delivery `channel`+`destination`, `is_active`). `rule_type`/`channel` are
  CHECK-constrained rather than native Postgres enums, matching the rest of
  this schema's plain-`String` style — avoids the migration ceremony of
  altering a Postgres enum type later (this mattered in practice: adding
  `price_drop_pct` needed a hand-written constraint drop/recreate in the
  migration either way, since Alembic's autogenerate doesn't detect CHECK
  constraint *body* changes — but a native enum would have needed the same
  by-hand treatment plus more).
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
| GET    | `/metrics`                        | Prometheus text-format metrics (see **Metrics** below) |

## Metrics

The two `/metrics` endpoints, in standard Prometheus text exposition
format — scraped by an actual Prometheus server and visualized in
Grafana when deployed via `k8s/monitoring/` (see **Kubernetes** below);
docker-compose alone only exposes the endpoints, nothing scrapes them.
All definitions live in one
place, `shared/metrics.py`, imported by both services so they report under
identical metric names — see that module's docstring for the full
reasoning behind the gauge-refresh design in particular.

| Metric | Type | Labels | What it's for |
|--------|------|--------|----------------|
| `scrape_attempts_total` | Counter | `outcome`, `error_type` | Scrape volume and failure rate, sliceable by error type |
| `alerts_fired_total` | Counter | `rule_type`, `channel` | How often each alert rule type actually fires (fresh triggers + material re-triggers — not resolutions) |
| `alert_deliveries_total` | Counter | `channel`, `status` | Notification delivery success/failure rate, by channel |
| `jobs_enqueued_total` | Counter | — | Scrape jobs pushed onto the queue |
| `jobs_dead_lettered_total` | Counter | `reason` | Jobs that exhausted retries or failed terminally, by why |
| `queue_depth` | Gauge | — | Current main scrape queue length |
| `delayed_queue_depth` | Gauge | — | Jobs currently waiting out a retry backoff |
| `dead_letter_depth` | Gauge | — | Jobs currently in the dead letter queue |
| `products_tracked_total` | Gauge | `is_active` | Tracked products, active vs. inactive |
| `scrape_duration_seconds` | Histogram | — | How long a scrape attempt takes (buckets tuned 1-30s — Playwright launches a real browser, this is seconds not milliseconds) |
| `alert_delivery_duration_seconds` | Histogram | — | How long notification delivery takes, retries included |

**Counters and histograms** are incremented/observed inline, exactly where
the thing they measure happens — `worker_app/main.py` (scrapes),
`shared/alerts.py` (alert firing), `shared/notifiers.py` (deliveries),
`shared/queue.py` (enqueue). Standard stuff.

**Gauges are different in kind**, and it's worth knowing why before
changing them. They represent state that already lives somewhere else —
Redis list/zset/hash lengths, a Postgres row count — not something this
process accumulates. Setting them "on write" (+1 per enqueue, -1 per
dequeue) would mean re-deriving Redis's own state by tracking every
mutation perfectly, across however many processes are running, with no way
to self-heal from a missed decrement (a crash mid-job, a manually
`redis-cli`-poked queue, a replayed dead letter). Instead they're
refreshed by querying Redis/Postgres directly, fresh, every time metrics
are scraped (`shared/metrics.py::refresh_gauges`):

- The **API**'s `GET /metrics` handler is `async def` and `await`s
  `refresh_gauges()` directly — refreshed at *exactly* scrape time, every
  time.
- The **worker** has no HTTP server of its own to hang a per-request hook
  off — `prometheus_client.start_http_server` runs a plain background
  thread with no async entry point. So `worker_app/main.py`'s
  `metrics_refresh_loop` calls `refresh_gauges()` on a timer instead
  (every `GAUGE_REFRESH_INTERVAL_SECONDS`, default 5s) and the HTTP server
  just serves whatever was last set. A small, deliberate, documented gap
  from "exact scrape time" — a real Prometheus server polls every 15-30s
  anyway, so a value that's at most 5s stale isn't meaningfully different
  from a fresh one.

We considered a genuine custom `Collector` (registers so `collect()` runs
automatically on every scrape — the textbook-correct pattern here) and
didn't use it: `collect()` is synchronous in prometheus_client, and every
query it'd need is async in this codebase (`redis.asyncio`, async
SQLAlchemy). Bridging that means either a second set of sync
clients/drivers just for metrics, or nesting an event loop inside one
that's already running — which breaks specifically inside FastAPI's
request handler. Plain gauges refreshed from an already-async context, as
above, gets the same "not stale" property without either problem.

## Kubernetes

Plain YAML manifests under `k8s/` — no Helm, no Kustomize — deploy the
same stack (Postgres, Redis, the migration, the api, the worker) to a
local minikube cluster. Full deploy order, secret handling, and common
`kubectl` commands are in [`k8s/README.md`](k8s/README.md); the short
version:

```bash
minikube start --cpus=4 --memory=6000mb
docker compose build api worker
minikube image load daraz-price-tracker-api:latest
minikube image load daraz-price-tracker-worker:latest

kubectl apply -f k8s/namespace.yaml -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml -f k8s/secret.yaml
kubectl apply -f k8s/postgres-statefulset.yaml -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/migrate-job.yaml
kubectl apply -f k8s/api-deployment.yaml -f k8s/worker-deployment.yaml

kubectl get all -n price-tracker
kubectl port-forward -n price-tracker svc/api 8000:8000
```

Notable design points (each has a longer comment at its source):

- **Postgres and Redis are StatefulSets** with `volumeClaimTemplates`
  (5Gi / 1Gi) and headless Services (`clusterIP: None`) — a StatefulSet
  needs one to give its pod(s) a stable per-pod DNS identity instead of
  being load-balanced behind a single cluster IP.
- **Migrations run as a one-shot Job**, not inside the api Deployment's
  own startup — `alembic upgrade head` must run exactly once per rollout,
  not once per replica racing the others.
- **The api Deployment has two probes with different jobs**: readiness on
  `/health` (checks the DB — an unreachable-DB pod shouldn't get traffic)
  and liveness on `/live` (deliberately DB-free — a Postgres blip
  shouldn't make Kubernetes kill every api pod at once).
- **The worker Deployment has no readiness probe** — it pulls jobs from
  Redis on its own initiative rather than receiving traffic a Service
  routes to it, so there's nothing for readiness to gate. Its liveness
  probe hits its own `:9100/metrics`.
- **`terminationGracePeriodSeconds: 90` on the worker**, and
  `worker_app/main.py` now traps `SIGTERM` to stop pulling new jobs and
  let the in-flight `process_job()` call finish before exiting — Redis has
  no separate record of an in-flight job, so a hard kill mid-scrape loses
  that job outright rather than retrying it.

### Monitoring (Prometheus + Grafana)

Deployed into a separate `monitoring` namespace under `k8s/monitoring/`
— full details in [`k8s/README.md`](k8s/README.md#monitoring-prometheus--grafana).
Short version:

```bash
kubectl apply -f k8s/monitoring/namespace.yaml
kubectl apply -f k8s/monitoring/prometheus-rbac.yaml
kubectl apply -f k8s/monitoring/prometheus-config.yaml
kubectl apply -f k8s/monitoring/prometheus-deployment.yaml
kubectl apply -f k8s/monitoring/grafana-config.yaml
kubectl apply -f k8s/monitoring/grafana-secret.yaml
kubectl apply -f k8s/monitoring/grafana-dashboards.yaml
kubectl apply -f k8s/monitoring/grafana-deployment.yaml

kubectl port-forward -n monitoring svc/prometheus 9090:9090   # http://localhost:9090/targets
kubectl port-forward -n monitoring svc/grafana 3000:3000      # http://localhost:3000 (admin/changeme)
```

Prometheus finds api/worker pods via Kubernetes service discovery
(`kubernetes_sd_configs`, role: pod) — it lists pods across the cluster
using a ClusterRole and keeps only the ones annotated
`prometheus.io/scrape: "true"` (already set on both Deployments in
`k8s/`). No static target list, no Prometheus config change to add a
target: scale a Deployment up and the new pod is a scrape target within
one interval (15s).

Two dashboards are provisioned from JSON in `k8s/monitoring/grafana-dashboards.yaml`,
not clicked together in the UI:

- **Scraper Health** — scrape rate (success vs. failure), success rate %,
  p50/p95/p99 scrape duration, failures by `error_type`, dead letter
  queue depth.
- **Queue & Workers** — main queue depth, delayed retry queue depth,
  jobs-enqueued-rate vs. jobs-processed-rate (a persistent gap between
  the two is the under-provisioned signal), alerts fired by rule type,
  alert delivery success/failure by channel.

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

Last local run: **105 passed, 84.4% coverage** (`--cov-fail-under=70`,
enforced in CI). `shared/metrics.py` and `api/app/routers/metrics.py` are
both at 100%. What's *not* covered elsewhere, and why that's an accepted
gap rather than an oversight:

- **`worker_app/main.py`'s `worker_loop`, `promoter_loop`,
  `metrics_refresh_loop`, and `run()`** (this file is at 71%, up from 55%
  before `test_metrics.py` started exercising `process_job` more directly)
  — the remaining gap is entirely these `while True:` loops wrapping
  functions that *are* unit-tested. Unit-testing an infinite loop means
  either breaking out of it artificially (tests the harness, not the
  code) or an integration test that starts a real worker process. These
  were verified by hand against the live stack (real 404 → terminal, real
  backoff growth in Redis, real 5-attempt exhaustion → DLQ → replay, and —
  this phase — real counters moving on a real `docker compose` stack),
  just not by anything `pytest` runs. Worth an integration test with a
  real stack if this becomes CI-gated later; not worth faking here.
- **`shared/notifiers.py`** (62%, up from 28% — `test_metrics.py` now
  exercises `send_notification`'s success/failure/counter-recording paths
  directly) — what's left uncovered is specifically the real
  `aiosmtplib`/`httpx` I/O inside `_send_email`/`_send_webhook` (including
  the Discord-vs-generic payload branch) and the multi-attempt
  retry-with-backoff loop in `_send_with_retries`, none of which `pytest`
  exercises end-to-end per the task's "no real SMTP/HTTP in tests"
  instruction. All of it *is* real-verified, just not by `pytest`: genuine
  SMTP traffic to Mailhog (confirmed via its API) for email, and a genuine
  POST to a real Discord webhook (confirmed by `delivery_status="sent"`
  with no `delivery_error`, and visually in Discord) for the
  webhook/Discord path. Same "live-verified, not pytest-verified"
  situation as the worker loops above.
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
│   │   │   ├── metrics.py      # /metrics
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
│   │   ├── main.py          # dequeue/promoter/metrics-refresh loops, retry decisions, alert evaluation
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
│   ├── alerts.py             # rule evaluation + dedup engine
│   └── metrics.py            # Prometheus counters/gauges/histograms, refresh_gauges()
├── tests/
│   ├── conftest.py          # test DB + fakeredis + httpx client fixtures
│   ├── test_retry.py        # backoff math, jitter, max-attempts
│   ├── test_error_classification.py  # exception hierarchy, mocked Playwright
│   ├── test_queue.py        # enqueue/dequeue/retry/DLQ, via fakeredis
│   ├── test_url_utils.py    # daraz_url normalization/validation
│   ├── test_alerts.py       # rule firing, dedup, cooldown, resolution
│   ├── test_api.py          # FastAPI endpoints, via httpx ASGI transport
│   └── test_metrics.py      # counters/gauges/histograms, /metrics format
├── k8s/                      # plain-YAML manifests for a local minikube deploy
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secret.yaml           # placeholders only — see k8s/README.md
│   ├── rbac.yaml
│   ├── postgres-statefulset.yaml
│   ├── redis-statefulset.yaml
│   ├── migrate-job.yaml
│   ├── api-deployment.yaml   # prometheus.io/* scrape annotations included
│   ├── worker-deployment.yaml # prometheus.io/* scrape annotations included
│   ├── monitoring/            # separate "monitoring" namespace
│   │   ├── namespace.yaml
│   │   ├── prometheus-rbac.yaml
│   │   ├── prometheus-config.yaml     # scrape + relabel_configs
│   │   ├── prometheus-deployment.yaml
│   │   ├── grafana-config.yaml        # datasource + dashboard-provider provisioning
│   │   ├── grafana-dashboards.yaml    # the 2 dashboards, as JSON
│   │   ├── grafana-secret.yaml        # placeholders only
│   │   └── grafana-deployment.yaml
│   └── README.md             # deploy order, secrets, common kubectl commands
├── .github/workflows/ci.yml # test (matrix) -> lint -> build, gha layer cache
├── pyproject.toml           # pytest, coverage, ruff config
├── requirements-dev.txt
├── docker-compose.yml
├── .env.example
└── .gitignore
```
