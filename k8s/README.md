# Kubernetes manifests

Plain YAML, no Helm/Kustomize — every object here is meant to be read
directly. Targets a local `minikube` cluster with images built and loaded
locally (not pulled from a registry).

## What's here

| File | Object(s) |
|---|---|
| `namespace.yaml` | `price-tracker` namespace — everything else lives in it |
| `configmap.yaml` | non-secret app config |
| `secret.yaml` | placeholder credential shape — **never holds real values in git** |
| `rbac.yaml` | ServiceAccount + Role + RoleBinding so the api pod's initContainer can poll the migrate Job's status |
| `postgres-statefulset.yaml` | Postgres StatefulSet + headless Service + PVC |
| `redis-statefulset.yaml` | Redis StatefulSet (AOF persistence) + headless Service + PVC |
| `migrate-job.yaml` | one-shot `alembic upgrade head` Job |
| `api-deployment.yaml` | api Deployment (2 replicas, `prometheus.io/*` scrape annotations) + ClusterIP Service |
| `worker-deployment.yaml` | worker Deployment (2 replicas, `prometheus.io/*` scrape annotations) + headless Service for metrics scraping |
| `worker-hpa.yaml` | HorizontalPodAutoscaler for `worker`, scaling on `queue_depth` — see **Autoscaling** below |

No Mailhog manifest — email-channel alerts aren't wired up in this
cluster (see the `SMTP_HOST` comment in `configmap.yaml`). Nothing else
depends on it.

`k8s/monitoring/` is a separate namespace (`monitoring`) with its own
deploy order — see **Monitoring (Prometheus + Grafana)** below.

## Building and loading images

minikube's Docker daemon is separate from your host's — images built with
`docker compose build` (or plain `docker build`) don't exist inside the
cluster until you load them explicitly:

```bash
docker compose build api worker
minikube image load daraz-price-tracker-api:latest
minikube image load daraz-price-tracker-worker:latest
```

All Deployments/Jobs use `imagePullPolicy: IfNotPresent` — if you skip
`minikube image load` after changing code, pods will silently keep
running the old image instead of erroring, so re-load after every rebuild.

## Deploy order

Namespace and RBAC first (everything else references them), then config,
then stateful services, then the migration, then the apps:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml          # see "Secrets" below first
kubectl apply -f k8s/postgres-statefulset.yaml
kubectl apply -f k8s/redis-statefulset.yaml

kubectl wait --for=condition=ready pod -l app=postgres -n price-tracker --timeout=120s

kubectl apply -f k8s/migrate-job.yaml
kubectl wait --for=condition=complete job/migrate -n price-tracker --timeout=120s

kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/worker-deployment.yaml
```

The api Deployment's initContainer also waits for the migrate Job on its
own, so applying it before the Job finishes isn't fatal — it just sits in
`Init:0/1` until migrations complete. The explicit `kubectl wait` above is
just so you see the state transitions rather than a pod parked mid-init.

## Secrets

`k8s/secret.yaml` as committed only has placeholder values — safe to
apply as-is on a **throwaway/dev cluster** like this minikube deploy, but
never edit it in place to hold a real credential. For anything beyond
local dev, create the Secret imperatively instead so the real value never
touches a YAML file or git history:

```bash
kubectl create secret generic price-tracker-secrets \
  --namespace price-tracker \
  --from-literal=POSTGRES_PASSWORD='<real password>' \
  --from-literal=DATABASE_URL='postgresql+asyncpg://daraz:<real password>@postgres:5432/daraz_price_tracker' \
  --from-literal=DISCORD_WEBHOOK_URL='<real discord webhook url, or empty>' \
  --from-literal=SMTP_USERNAME='<real smtp username, or empty>' \
  --from-literal=SMTP_PASSWORD='<real smtp password, or empty>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

If you change `POSTGRES_PASSWORD`, `DATABASE_URL`'s embedded password
must match — they're two separate keys because the postgres container
only wants the bare password, while api/worker/migrate want the full
connection string.

## Monitoring (Prometheus + Grafana)

Separate namespace (`monitoring`), separate deploy step, deployed after
the `price-tracker` namespace exists (it scrapes pods in it — nothing
about deploy order between the two matters for Postgres/Redis/migrate,
just that api/worker pods need to already be annotated, which they are as
committed).

| File | Object(s) |
|---|---|
| `monitoring/namespace.yaml` | `monitoring` namespace |
| `monitoring/prometheus-rbac.yaml` | ServiceAccount + ClusterRole + ClusterRoleBinding — Prometheus needs cluster-wide `get/list/watch` on pods/services/endpoints/nodes to run its own service discovery (see the comment in that file for what a missing-RBAC failure actually looks like) |
| `monitoring/prometheus-config.yaml` | scrape config: `kubernetes_sd_configs` (role: pod) + `relabel_configs` that keep only `prometheus.io/scrape: "true"` pods — heavily commented, since relabeling is the part of a Prometheus config that's hardest to read back later |
| `monitoring/prometheus-deployment.yaml` | Prometheus Deployment + PVC (7d retention) + ClusterIP Service |
| `monitoring/grafana-config.yaml` | datasource provisioning (points at the Prometheus Service by DNS name) + dashboard-provider config, both as ConfigMaps — no UI click-ops |
| `monitoring/grafana-dashboards.yaml` | the two dashboards themselves, as JSON, provisioned from this ConfigMap |
| `monitoring/grafana-secret.yaml` | placeholder admin credentials — same rule as `k8s/secret.yaml`, never real values in git |
| `monitoring/grafana-deployment.yaml` | Grafana Deployment + PVC + ClusterIP Service |

Deploy:

```bash
kubectl apply -f k8s/monitoring/namespace.yaml
kubectl apply -f k8s/monitoring/prometheus-rbac.yaml
kubectl apply -f k8s/monitoring/prometheus-config.yaml
kubectl apply -f k8s/monitoring/prometheus-deployment.yaml

kubectl apply -f k8s/monitoring/grafana-config.yaml
kubectl apply -f k8s/monitoring/grafana-secret.yaml
kubectl apply -f k8s/monitoring/grafana-dashboards.yaml
kubectl apply -f k8s/monitoring/grafana-deployment.yaml
```

Reach both UIs via port-forward (no Ingress in this cluster — see "Known
gaps"):

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090
# → http://localhost:9090/targets  (Status > Targets) to see discovered
#   api/worker pods and their UP/DOWN state

kubectl port-forward -n monitoring svc/grafana 3000:3000
# → http://localhost:3000  (user/password from grafana-secret.yaml —
#   "admin" / "changeme" as committed, unless you replaced the Secret)
# Dashboards live under the "Price Tracker" folder: "Scraper Health" and
# "Queue & Workers".
```

Adding a new scrape target later needs no Prometheus config change at
all — just the three `prometheus.io/*` annotations on the pod template
(see `api-deployment.yaml`/`worker-deployment.yaml`); the next scrape
interval (15s) picks it up automatically via service discovery.

## Autoscaling (prometheus-adapter + HPA)

The metrics chain end to end: **app `/metrics`** (Prometheus text format,
`shared/metrics.py`) → **Prometheus** scrapes it (above) → **prometheus-adapter**
runs a PromQL query against Prometheus and reshapes the result into the
Kubernetes custom metrics API's JSON shape → the **custom.metrics.k8s.io**
aggregated API (registered via an `APIService`, served by the adapter, not
the main apiserver) → the **HPA controller** (inside kube-controller-manager)
polls that API on its normal sync period and adjusts `worker`'s replica
count. Five hops, and any one of them can be silently broken while the
others look fine — see the verification steps below, not just "the pods
are Running."

| File | Object(s) |
|---|---|
| `monitoring/prometheus-adapter/rbac.yaml` | ServiceAccount + the three separate RBAC grants an aggregated API server needs (see the long comment at the top of that file: incoming-request validation, the adapter's own k8s API reads, and — easy to miss — the HPA controller's own permission to call `custom.metrics.k8s.io` at all) |
| `monitoring/prometheus-adapter/config.yaml` | the adapter's rules: `queue_depth`, `dead_letter_depth` (both `max()`, not `sum()` — see that file's header comment for why summing would runaway), `scrape_duration_seconds_p95` (correctly `sum()`-then-quantile, since histogram buckets *are* additive per pod) |
| `monitoring/prometheus-adapter/apiservice.yaml` | registers `v1beta1.custom.metrics.k8s.io` with the main apiserver, pointed at the adapter's Service |
| `monitoring/prometheus-adapter/deployment.yaml` | adapter Deployment + Service (serves HTTPS on :6443, self-signed cert) |
| `worker-hpa.yaml` | the HPA itself — `queue_depth` as an Object metric (not Pods — see the comment in that file for why Pods would double-count), `AverageValue: "10"`, min 2 / max 10, asymmetric scale-up/scale-down `behavior` |

Deploy (after Prometheus is already up):

```bash
kubectl apply -f k8s/monitoring/prometheus-adapter/rbac.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/config.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/deployment.yaml
kubectl apply -f k8s/monitoring/prometheus-adapter/apiservice.yaml
kubectl apply -f k8s/worker-hpa.yaml
```

Verify each hop actually works — this is the phase most likely to *look*
right (pods Running, APIService registered) while actually being wrong
(RBAC missing one binding, HPA stuck on `<unknown>`):

```bash
# 1. the APIService registration itself
kubectl get apiservice v1beta1.custom.metrics.k8s.io
# STATUS should be "True" (Available) within ~30s

# 2. the API is actually being served, not just registered
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | python3 -m json.tool

# 3. a real value comes back, matching Prometheus directly
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/price-tracker/metrics/queue_depth" | python3 -m json.tool
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl -s 'localhost:9090/api/v1/query?query=max(queue_depth)' | python3 -m json.tool
# both numbers should match — this is what actually proves max() is
# collapsing the per-pod duplicates instead of summing them

# 4. the HPA controller can reach it too (the RBAC binding that's easiest to miss)
kubectl describe hpa worker -n price-tracker
# "Metrics:" line should show a real number, not <unknown>

kubectl get hpa worker -n price-tracker -w
```

Load-test the scale-up (see the main README's Kubernetes section for the
full walkthrough with real numbers): enqueue a large burst of scrape jobs
so `queue_depth` climbs well past `averageValue: "10"`, then watch
`kubectl get hpa -w` and `kubectl get pods -n price-tracker -l app=worker -w`
— replicas should climb quickly (no stabilization window on scale-up).
Once the queue drains, replicas come back down much more slowly (5-minute
stabilization window, at most 1 pod removed per minute) — that asymmetry
is deliberate, see the comment in `worker-hpa.yaml`.

## Common commands

```bash
# overall state
kubectl get all -n price-tracker

# pod/Job status and events (useful when something's stuck in Init or CrashLoopBackOff)
kubectl get pods -n price-tracker
kubectl describe pod <pod-name> -n price-tracker
kubectl get job migrate -n price-tracker

# logs
kubectl logs -n price-tracker deploy/api --tail 50
kubectl logs -n price-tracker deploy/worker --tail 50 -f
kubectl logs -n price-tracker job/migrate

# reach the API from your host
kubectl port-forward -n price-tracker svc/api 8000:8000
curl localhost:8000/health

# reach a worker's metrics from your host
kubectl port-forward -n price-tracker pod/<worker-pod-name> 9100:9100
curl localhost:9100/metrics

# open a psql shell against the running Postgres pod
kubectl exec -it -n price-tracker postgres-0 -- psql -U daraz -d daraz_price_tracker

# re-deploy after a code change
docker compose build api worker
minikube image load daraz-price-tracker-api:latest
minikube image load daraz-price-tracker-worker:latest
kubectl rollout restart deployment/api deployment/worker -n price-tracker

# tear down everything in the namespace (keeps the namespace itself)
kubectl delete all --all -n price-tracker
# tear down completely, including the PVCs (destroys the database!)
kubectl delete namespace price-tracker

# monitoring stack
kubectl get pods -n monitoring
kubectl logs -n monitoring deploy/prometheus --tail 50
kubectl logs -n monitoring deploy/grafana --tail 50
# scale workers and watch Prometheus pick up the new pod (no config change)
kubectl scale deployment/worker -n price-tracker --replicas=4
kubectl delete namespace monitoring   # tear down monitoring only

# autoscaling
kubectl get apiservice v1beta1.custom.metrics.k8s.io
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | python3 -m json.tool
kubectl describe hpa worker -n price-tracker
kubectl get hpa worker -n price-tracker -w
kubectl logs -n monitoring deploy/custom-metrics-apiserver --tail 50
```

## Known gaps (out of scope for this phase)

- No Ingress — both the API and the monitoring UIs are reached via
  `kubectl port-forward` or `minikube service`, not a real hostname.
- No HPA on the api Deployment — only `worker` scales, on `queue_depth`.
  api's own load characteristics (request rate, latency) would need a
  different metric and weren't part of this phase's ask.
- No NetworkPolicy — every pod in the namespace can reach every other pod.
- Single-replica Postgres/Redis — no failover if that pod's node goes down.
- Single-replica Prometheus/Grafana too, same caveat, plus: Prometheus's
  TSDB PVC is `ReadWriteOnce` — fine for one replica, would need a
  different storage class (or a remote-write setup) before this could
  ever run more than one Prometheus pod.
- No alerting rules configured in Prometheus (Alertmanager isn't
  deployed either) — the two Grafana dashboards are for humans looking,
  not paging anyone yet.
