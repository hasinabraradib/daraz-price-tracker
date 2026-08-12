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
| `api-deployment.yaml` | api Deployment (2 replicas) + ClusterIP Service |
| `worker-deployment.yaml` | worker Deployment (2 replicas) + headless Service for metrics scraping |

No Mailhog manifest — email-channel alerts aren't wired up in this
cluster (see the `SMTP_HOST` comment in `configmap.yaml`). Nothing else
depends on it.

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
```

## Known gaps (out of scope for this phase)

- No Ingress — the API is reached via `kubectl port-forward` or
  `minikube service`, not a real hostname.
- No Prometheus/Grafana deployed to scrape the metrics endpoints — the
  worker's headless `worker-metrics` Service is discoverable and ready
  for a `kubernetes_sd_config` scrape job, but nothing is scraping it yet.
- No HorizontalPodAutoscaler — the api/worker resource `requests` are set
  (an HPA requires them) but no HPA object exists yet.
- No NetworkPolicy — every pod in the namespace can reach every other pod.
- Single-replica Postgres/Redis — no failover if that pod's node goes down.
