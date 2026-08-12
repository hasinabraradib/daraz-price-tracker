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
```

## Known gaps (out of scope for this phase)

- No Ingress — both the API and the monitoring UIs are reached via
  `kubectl port-forward` or `minikube service`, not a real hostname.
- No HorizontalPodAutoscaler — the api/worker resource `requests` are set
  (an HPA requires them) but no HPA object exists yet.
- No NetworkPolicy — every pod in the namespace can reach every other pod.
- Single-replica Postgres/Redis — no failover if that pod's node goes down.
- Single-replica Prometheus/Grafana too, same caveat, plus: Prometheus's
  TSDB PVC is `ReadWriteOnce` — fine for one replica, would need a
  different storage class (or a remote-write setup) before this could
  ever run more than one Prometheus pod.
- No alerting rules configured in Prometheus (Alertmanager isn't
  deployed either) — the two Grafana dashboards are for humans looking,
  not paging anyone yet.
