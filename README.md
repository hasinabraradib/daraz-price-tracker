# daraz-price-tracker

[![CI](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/hasinabraradib/daraz-price-tracker/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-84.4%25-brightgreen)

> The CI badge above will 404 until this repo exists at
> `github.com/hasinabraradib/daraz-price-tracker` and the workflow has run
> at least once. The coverage badge is a static snapshot from the last
> local run (see **Tests** below), not a live/auto-updating one; wiring up
> Codecov or a CI step that regenerates it is a natural follow-up if you
> want that.

Tracks product listings on Daraz (Bangladesh's largest online marketplace)
and tells you when something changes. One pipeline, two audiences: sellers
who want to know the moment a competitor undercuts their price, and
shoppers who want to know the moment a price drops below a threshold or an
out-of-stock item comes back. A FastAPI service takes requests, a
Redis-queued worker drives headless Chromium to actually scrape, Postgres
holds the history, and alerts go out over email, Discord, or a generic
webhook. Deployable via Docker Compose locally, Kubernetes (with
Prometheus/Grafana and a queue-depth-driven autoscaler) for a fuller demo,
or one cost-minimized AWS instance via Terraform.

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
of database and queue code — see **Shared code** below. That's the app
itself; deployed to Kubernetes (`k8s/`, either minikube or the Terraform-provisioned
EC2 instance) it looks like this:

```
Kubernetes (k8s/) — namespace price-tracker
                                       ┌──▶ HPA (min 2, max 10)
  api ×2 ──▶ postgres (StatefulSet)    │        on queue_depth
  worker ×2 ──▶ redis (StatefulSet) ───┘        (minikube only — see Limitations)
       │
       │ /metrics (api :8000, worker :9100)
       ▼
namespace monitoring
  Prometheus ── scrapes api/worker pods automatically (prometheus.io/scrape annotations)
      │
      ├──▶ Grafana            (Scraper Health, Queue & Workers dashboards)
      └──▶ prometheus-adapter ──▶ custom.metrics.k8s.io ──▶ feeds the HPA above
```

## Engineering notes

The decisions worth defending, condensed — full reasoning and code
pointers for each live in their own section further down.

**Error classification.** Not every scrape failure deserves the same
response. A 404 (product gone) will never succeed no matter how many
times you retry it — retrying anyway just burns attempt budget on
something that can't be fixed by trying again, so `TerminalScrapeError`
dead-letters immediately, no retry. A missing selector is different but
still capped hard: it gets exactly one retry, because if the *next*
attempt fails the same way, that's not noise — it means Daraz's markup
changed, and no amount of further retrying fixes stale selectors. See
**Queue, retries, and the dead letter queue** below.

**Backoff lives in a Redis sorted set, not `time.sleep()`.** A sleeping
retry blocks the worker from processing anything else while it waits, and
the wait is lost entirely if the worker restarts mid-backoff. A retry is
written instead to a Redis zset (score = the unix timestamp it becomes
eligible); a second coroutine polls it every 2s and promotes due jobs back
onto the main queue. The worker keeps working while a job waits, and the
wait survives a restart because it was never in-process state to begin
with.

**Full jitter, not pure exponential backoff.** Pure exponential backoff
with no randomness means every job that failed at the same moment (say,
Daraz having a bad minute) also *retries* at the same moment — a
self-inflicted thundering herd against an upstream that's already
struggling. `random.uniform(0, min(base * factor^attempt, max))` spreads
those retries across a window instead of a single instant.

**Alert dedup: open/resolved state, and why `price_below` measures the
gap, not the price.** Naively re-notifying every scrape a condition is
still true floods people; never re-notifying at all misses real new
information. An `AlertEvent` stays open while `resolved_at IS NULL`, and
only a *material* change — not just "still true" — closes it and opens a
fresh one. For `price_below`, materiality is percent change in
`(threshold − price)`, not in the raw price: a threshold of 1000 going
990 → 980 is a ~1% raw move, but the gap below threshold *doubles* (10 →
20) — materially further below, and worth telling someone even though the
price itself barely moved.

**`price_drop_pct` is edge-triggered; the others are level-triggered.** A
"price dropped by X%" condition doesn't have a sensible answer to "is it
*still* true" — still true compared to what baseline? So it isn't treated
that way: it's defined purely in terms of the two most recent snapshots,
true only on the exact scrape where a qualifying drop happens, then
self-resolves immediately. It can't be true again next scrape unless
another real drop occurs, since "previous" always advances — no
open/resolved state machine needed, cooldown alone guards against
repeats.

**Liveness must not check the database.** If the liveness probe checks
DB connectivity and Postgres has a brief blip, every API pod fails
liveness at the same instant and Kubernetes kills all of them
simultaneously — then restarts them into the same still-broken database.
That's a self-inflicted total outage stacked on top of the original blip,
and restarting a healthy process doesn't fix a database problem anyway.
Readiness checks the DB (an unreachable-DB pod is pulled from rotation,
not killed); liveness deliberately doesn't.

**Per-process Prometheus registries: `sum()` for counters, `max()` for
gauges reading shared state.** Each pod's counter (scrape attempts, jobs
enqueued) really is that pod's own share of the total — `sum()` across
pods is correct. But `queue_depth`/`dead_letter_depth` are gauges read
from Redis, and *every* pod reports the same cluster-wide number — `sum()`
across N pods would multiply the true value by N. This isn't just a
dashboard cosmetics issue: an adapter rule using `sum()` instead of
`max()` would inflate the value the HPA sees on every scale-up (more pods
→ higher summed value → scale up further), a feedback loop with no
ceiling short of `maxReplicas`.

**HPA on queue depth, not CPU.** A worker idling between Playwright
launches looks identical on CPU whether the queue has 2 jobs or 2000 —
CPU-based autoscaling wouldn't track actual demand for this workload at
all. Queue depth is the metric that actually reflects "is there more work
waiting than the current workers can handle," which is worth a 5-hop
metrics chain (app → Prometheus → prometheus-adapter →
`custom.metrics.k8s.io` → HPA controller) to get in front of the
autoscaler correctly.

**k3s on one EC2 instance, not EKS.** EKS's control plane alone is a flat
~$73/month before a single worker node exists — for a single-node
portfolio demo, that buys zero additional demonstration value over a free
alternative. k3s is fully conformant Kubernetes (same API, same RBAC,
same everything) running as one process — every manifest under `k8s/`
applies to it completely unchanged, so "is this really Kubernetes" has a
clean, verifiable yes without paying for a managed control plane this demo
will never come close to stressing.

## Limitations

Stated plainly, not buried:

- **No scheduler.** Scrapes are triggered manually via
  `POST /products/{id}/scrape` — there's no cron/interval mechanism
  polling tracked products on its own. This is on-demand scraping with a
  full pipeline behind it (retries, alerting, metrics) once triggered, not
  yet continuous tracking. Adding one would mean a periodic loop (or a
  Kubernetes `CronJob` hitting the enqueue endpoint) iterating active
  products on some interval, respecting the polite delay and backoff
  already in place — probably with a per-product interval, since a seller
  checking hourly and a shopper checking daily want different cadences.
- **Stock detection is unverified against a real out-of-stock page.**
  `_detect_in_stock` in `worker/worker_app/scraper.py` is a best-effort
  heuristic (looks for an Add to Cart/Buy Now button, checks its text
  against sold-out markers) that has never actually been confirmed
  against a genuinely out-of-stock Daraz product — every product used in
  testing stayed in stock throughout. Flagged in the scraper's own
  docstring; worth verifying before trusting `back_in_stock` alerts in a
  way that matters.
- **Tested at single-product volume; no proxy rotation.** Verification
  throughout has been one to a handful of real products, sequential
  requests, well under anything that would trigger bot detection. At real
  scale — hundreds/thousands of products, more frequent polling — this
  would hit Daraz's bot detection without proxy rotation, IP diversity,
  or more sophisticated request patterns, none of which exist here.
  `POLITE_DELAY_SECONDS` self-throttles per worker process; it doesn't
  solve for scale.
- **A single prometheus-adapter replica timed out under combined load
  during testing.** During the HPA load test (a 200-job burst plus 10
  worker pods starting simultaneously on one minikube node), the adapter
  pod logged `Handler timeout` errors for a few seconds under the
  resource pressure. It self-recovered with no restart and the HPA
  resumed normally right after — one replica was adequate for the demo,
  but it's a documented single point of failure. Production would run
  two.
- **Scraping public pages only, no CAPTCHA bypass — and the ToS question
  doesn't go away because of that.** This scrapes publicly accessible
  Daraz product pages, at low volume, with no attempt to circumvent
  access controls. That's still scraping a commercial site without an API
  agreement, which most sites' Terms of Service technically prohibit
  regardless of volume or politeness. Worth being direct about rather
  than implying that low volume and no bot-detection bypass resolve the
  ToS question — they don't.

## What I'd do next

- **A scheduler** — see **Limitations** above for the shape of it. This
  is the single biggest gap between "on-demand scraping tool" and
  "continuous price tracker," which is the actual pitch.
- **Messenger/WhatsApp alert channels** — more relevant than email for
  the actual target audience here (Bangladesh); email isn't most
  shoppers' first check, Messenger and WhatsApp are. Same shape as the
  existing Discord integration: a new delivery function behind
  `send_notification()`'s existing interface (`shared/notifiers.py`), a
  new `channel` value, no changes needed to the alert evaluation/dedup
  engine itself.
- **Real auth for multi-tenancy** — what exists today (`owner_email` on
  `Product`/`AlertRule`, an unverified `X-Owner-Email` header the `web/`
  frontend sends — see **Frontend** below) is a filtering label, not
  access control: nothing stops anyone from typing anyone else's email
  and seeing/controlling their products. Real multi-tenancy needs a
  `User` model, actual authentication (JWT or session-based), and every
  query scoped to a *verified* identity instead of a client-supplied
  string.

---

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
  visualized in Grafana when deployed to Kubernetes (see **Kubernetes**
  below) — docker-compose alone doesn't run either
- prometheus-adapter + HorizontalPodAutoscaler — `worker` autoscales on
  `queue_depth` on minikube (see **Autoscaling** under **Kubernetes**
  below) — no autoscaling in docker-compose, obviously
- Next.js 14 (App Router) + TypeScript + Tailwind — a minimal frontend in
  `web/`, see **Frontend** below
- Docker Compose for local dev, Kubernetes manifests for a local minikube
  deploy, Terraform for a real (cost-minimized) AWS deploy — see
  **Kubernetes** and **Terraform** below

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

## Frontend

A minimal Next.js frontend in `web/` — three pages (setup, add product,
dashboard), plain `useState`/`fetch`, no auth or state-management
library — built so someone who won't read the code can see the system
actually working: add a product, link a competitor, set up an alert
rule, trigger a real scrape, watch real data land on the dashboard. Full
detail (design notes, page-by-page behavior, what "not authentication"
actually means here) in [`web/README.md`](web/README.md). Short version:

```bash
cd web
npm install
cp .env.local.example .env.local   # NEXT_PUBLIC_API_URL, defaults to localhost:8000
npm run dev
```

Open http://localhost:3000 — the API needs to already be running (above)
and its `FRONTEND_ORIGIN` (repo-root `.env`) needs to match wherever this
runs; both default to matching localhost ports, so local dev needs no
extra config either side.

The frontend needs two small backend additions layered on top of
everything else in this README, both scoped tightly to what it actually
needed:

- **`owner_email` on `Product`/`AlertRule`**, set from an optional
  `X-Owner-Email` request header (`api/app/deps.py::get_owner_email`) so
  multiple people can demo this at once without each seeing everyone
  else's products by default. **This is not authentication** — there's
  no password, no token, nothing verifying the header's sender actually
  controls that email. Absent header = no filtering (existing behavior
  intact); a header present filters to that owner's rows plus anything
  with no recorded owner, never hiding pre-ownership data. See **What
  I'd do next** above for what real auth here would require.
- **CORS** (`api/app/main.py`) and **`GET /products?daraz_url=...`**
  (an exact-match lookup, bypassing the owner filter, used by the
  "link a competitor by URL" flow — see `web/README.md` for why
  `POST /products/{id}/competitors` alone isn't enough for that).

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

Notable design points not already covered in **Engineering notes**
(each has a longer comment at its source):

- **Postgres and Redis are StatefulSets** with `volumeClaimTemplates`
  (5Gi / 1Gi) and headless Services (`clusterIP: None`) — a StatefulSet
  needs one to give its pod(s) a stable per-pod DNS identity instead of
  being load-balanced behind a single cluster IP.
- **Migrations run as a one-shot Job**, not inside the api Deployment's
  own startup — `alembic upgrade head` must run exactly once per rollout,
  not once per replica racing the others.
- **The worker Deployment has no readiness probe** — it pulls jobs from
  Redis on its own initiative rather than receiving traffic a Service
  routes to it, so there's nothing for readiness to gate. Its liveness
  probe hits its own `:9100/metrics`.
- **`terminationGracePeriodSeconds: 90` on the worker**, and
  `worker_app/main.py` traps `SIGTERM` to stop pulling new jobs and let
  the in-flight `process_job()` call finish before exiting — Redis has no
  separate record of an in-flight job, so a hard kill mid-scrape loses
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

### Autoscaling (prometheus-adapter + HPA)

Full chain and design points in **Engineering notes** above; deploy
commands and verification steps:

```bash
kubectl apply -f k8s/monitoring/prometheus-adapter/rbac.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/config.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/deployment.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/apiservice.yaml
kubectl apply -f k8s/worker-hpa.yaml

kubectl get apiservice v1beta1.custom.metrics.k8s.io   # Available: True
kubectl describe hpa worker -n price-tracker           # a real number, not <unknown>
```

The RBAC in `prometheus-adapter/rbac.yaml` is the part most likely to
look fine and not be: the APIService can register as `Available: True`
and `kubectl get --raw` can work perfectly (you're cluster-admin) while
the HPA controller *itself* is still unauthorized to call
`custom.metrics.k8s.io` — it runs as its own ServiceAccount
(`system:serviceaccount:kube-system:horizontal-pod-autoscaler` on a
kubeadm-style cluster, which minikube is), and needs its own explicit
grant. That failure mode shows up as `kubectl describe hpa` sitting on
`<unknown>` forever, with no error surfaced on the HPA object itself —
see the k8s README's verification steps for how to check every hop
individually instead of trusting that "pods are Running" means the whole
chain works. (Not deployed on the Terraform/EC2 target — see
**Limitations** and **Terraform** below for why.)

## Terraform (AWS demo deploy)

Provisions one EC2 instance (`t3.small`, `eu-north-1`) running k3s and
deploys the same `k8s/` manifests to it — a real, publicly reachable demo
instead of a local cluster, at close to zero cost when torn down
promptly. Full prerequisites, apply/destroy workflow, and a billing-alarm
section are in [`terraform/README.md`](terraform/README.md); per-resource
cost breakdown (pulled live from the AWS Pricing API, not estimated) is
in [`terraform/COSTS.md`](terraform/COSTS.md). Short version:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set my_ip = $(curl -4 ifconfig.me)
terraform init
terraform plan                                  # review every resource before spending anything
terraform apply
./deploy.sh                                      # builds+ships images, applies k8s/, prints URLs
```

**Cost-minimized on purpose** — see **Engineering notes** above for the
k3s-vs-EKS reasoning specifically. Also skipped: a NAT gateway (~$32/month;
one public subnet only, nothing here needs the private-subnet isolation a
NAT gateway would exist to serve), RDS, and an ALB (a single EC2 instance
with an Elastic IP and k3s's bundled Traefik ingress is the entire
compute fleet — nothing here for a load balancer to balance across).
Real numbers: **~$0.024/hour, ~$17.44/month if left running, ~$0.07 for a
3-hour demo session.**

**Fit to the instance**: a `t3.small`'s 2GiB RAM doesn't have room for
the full stack at its local-minikube replica counts *plus*
prometheus-adapter/HPA — `deploy.sh` scales api/worker to 1 replica each
and deliberately skips the adapter/HPA (see **Limitations** above and the
comments in `deploy.sh`). Autoscaling is demonstrated on minikube
instead, where the node actually has room to scale into.

**Always `terraform destroy` when a demo session is done** — nothing this
creates keeps billing after that completes. See `terraform/COSTS.md`.

## Queue, retries, and the dead letter queue

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

## Scraper politeness

The worker scrapes one page at a time, throttled by `POLITE_DELAY_SECONDS`
(default 3s) between requests, targets public product pages only, and makes
no attempt to bypass bot detection or CAPTCHAs. See
`worker/worker_app/scraper.py` for details, and **Limitations** above for
the honest read on what this does and doesn't protect against at scale.

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
See **Engineering notes** above for why.

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

  See **What I'd do next** above for Messenger/WhatsApp, which would slot
  into this same interface.

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
  notification retries. Moved here from the worker specifically so
  notifiers could reuse it without a second implementation.
- `shared/notifiers.py` — `send_notification()`: email (SMTP via
  `aiosmtplib`) or webhook (`httpx` POST) delivery, retried via
  `shared/retry.py`.
- `shared/alerts.py` — `evaluate_alerts()`: the rule evaluation and dedup
  engine described above.
- `shared/metrics.py` — Prometheus counters/gauges/histograms shared by
  both services, plus `refresh_gauges()` — see **Metrics** below.

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
| GET    | `/products`                       | List products with their latest price. `?daraz_url=` switches to an exact-match lookup instead (used by `web/`'s "link a competitor by URL" flow) — see **Frontend** above |
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
| POST   | `/alerts/test-webhook`            | Fire a one-off test notification at a webhook URL, no product/rule involved — used by `web/`'s setup page |
| GET    | `/metrics`                        | Prometheus text-format metrics (see **Metrics** below) |
| GET    | `/live`                           | Liveness check — deliberately does not touch the DB (see **Engineering notes**) |

## Metrics

The two `/metrics` endpoints, in standard Prometheus text exposition
format — scraped by an actual Prometheus server and visualized in
Grafana when deployed via `k8s/monitoring/` (see **Kubernetes** above);
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

See **Engineering notes** above for why gauges specifically need `max()`
rather than `sum()` when aggregated across pods — the same issue shows up
identically in the Grafana dashboards and the prometheus-adapter rules
backing the HPA.

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
  backoff growth in Redis, real 5-attempt exhaustion → DLQ → replay, real
  counters moving on a real `docker compose` stack, and later a real
  Kubernetes cluster), just not by anything `pytest` runs. Worth an
  integration test with a real stack if this becomes CI-gated later; not
  worth faking here.
- **`shared/notifiers.py`** (62%, up from 28% — `test_metrics.py` now
  exercises `send_notification`'s success/failure/counter-recording paths
  directly) — what's left uncovered is specifically the real
  `aiosmtplib`/`httpx` I/O inside `_send_email`/`_send_webhook` (including
  the Discord-vs-generic payload branch) and the multi-attempt
  retry-with-backoff loop in `_send_with_retries`, none of which `pytest`
  exercises end-to-end per the "no real SMTP/HTTP in tests" rule this
  suite follows. All of it *is* real-verified, just not by `pytest`:
  genuine SMTP traffic to Mailhog (confirmed via its API) for email, and a
  genuine POST to a real Discord webhook (confirmed by
  `delivery_status="sent"` with no `delivery_error`, and visually in
  Discord) for the webhook/Discord path. Same "live-verified, not
  pytest-verified" situation as the worker loops above.
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
│   │   ├── main.py          # FastAPI app, router wiring, CORS, /health, /live
│   │   ├── deps.py          # get_owner_email, get_owned_product_or_404 — "Option B" ownership, not auth
│   │   ├── routers/
│   │   │   ├── products.py     # /products endpoints, incl. /attempts, ?daraz_url= lookup
│   │   │   ├── competitors.py  # /products/{id}/competitors, /comparison
│   │   │   ├── alert_rules.py  # /products/{id}/alert-rules
│   │   │   ├── alerts.py       # /alerts, /alerts/test-webhook
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
├── web/                      # minimal Next.js 14 frontend — see Frontend above
│   ├── app/
│   │   ├── page.tsx             # dashboard
│   │   ├── setup/page.tsx
│   │   └── products/new/page.tsx
│   ├── components/
│   │   ├── ui/                  # Button, Input, Card, Badge, Skeleton, EmptyState, ErrorState
│   │   ├── ProductCard.tsx
│   │   ├── PriceChart.tsx       # recharts
│   │   ├── CompetitorTable.tsx
│   │   ├── AlertRulesList.tsx
│   │   └── AlertEventsFeed.tsx
│   ├── lib/
│   │   ├── api.ts               # fetch wrapper, X-Owner-Email header, ApiError
│   │   ├── types.ts             # mirrors api/app/schemas.py
│   │   └── storage.ts           # localStorage: owner email, saved webhook url
│   └── README.md                # page-by-page detail, design notes, what "not auth" means
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
│   ├── worker-hpa.yaml        # HPA on queue_depth
│   ├── monitoring/            # separate "monitoring" namespace
│   │   ├── namespace.yaml
│   │   ├── prometheus-rbac.yaml
│   │   ├── prometheus-config.yaml     # scrape + relabel_configs
│   │   ├── prometheus-deployment.yaml
│   │   ├── grafana-config.yaml        # datasource + dashboard-provider provisioning
│   │   ├── grafana-dashboards.yaml    # the 2 dashboards, as JSON
│   │   ├── grafana-secret.yaml        # placeholders only
│   │   ├── grafana-deployment.yaml
│   │   └── prometheus-adapter/        # custom.metrics.k8s.io, backs the HPA
│   │       ├── rbac.yaml              # incl. the HPA controller's own grant
│   │       ├── config.yaml            # adapter rules: max() not sum(), see comments
│   │       ├── apiservice.yaml
│   │       └── deployment.yaml
│   └── README.md             # deploy order, secrets, common kubectl commands
├── terraform/                 # cost-minimized single-EC2-instance AWS deploy
│   ├── versions.tf            # provider, local-state comment
│   ├── variables.tf
│   ├── main.tf                # tags + "what we didn't provision, and why" comment
│   ├── network.tf             # VPC, 1 public subnet, IGW, security group
│   ├── compute.tf             # AMI data source, key pair, instance, EIP
│   ├── user_data.sh.tpl       # k3s + kubectl + helm bootstrap
│   ├── outputs.tf             # public IP, SSH command, kubeconfig fetch command
│   ├── manifests/
│   │   └── cloud-access.yaml  # Ingress + NodePort Services, cloud-only
│   ├── deploy.sh              # ships images, applies k8s/, prints URLs
│   ├── terraform.tfvars.example
│   ├── COSTS.md                # per-resource pricing, pulled from the AWS Pricing API
│   └── README.md               # prerequisites, apply/destroy workflow, billing alarm
├── .github/workflows/ci.yml # test (matrix) -> lint -> build, gha layer cache
├── pyproject.toml           # pytest, coverage, ruff config
├── requirements-dev.txt
├── docker-compose.yml
├── .env.example
└── .gitignore
```
