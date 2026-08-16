#!/usr/bin/env bash
# Run after `terraform apply` has finished (this script does not run
# terraform itself, and never does — see the "Do NOT run terraform apply"
# instruction this was built under; that's a human decision, not a script's).
#
# What it does, in order: fetch a working kubeconfig, build+ship the two
# app images onto the instance, apply k8s/ in the same dependency order
# k8s/README.md documents for minikube, fit the workload to a t3.small's
# 2GiB of RAM, apply the cloud-specific access layer, print the URLs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

cd "$SCRIPT_DIR"

PUBLIC_IP=$(terraform output -raw public_ip)
echo "==> instance: ubuntu@${PUBLIC_IP}"

echo "==> waiting for SSH..."
until ssh "${SSH_OPTS[@]}" "ubuntu@${PUBLIC_IP}" true 2>/dev/null; do
  sleep 5
done

echo "==> fetching kubeconfig (rewritten for ${PUBLIC_IP} — see user_data.sh.tpl's --tls-san)"
ssh "${SSH_OPTS[@]}" "ubuntu@${PUBLIC_IP}" "sudo cat /etc/rancher/k3s/k3s.yaml" \
  | sed "s/127.0.0.1/${PUBLIC_IP}/" > "$SCRIPT_DIR/kubeconfig"
export KUBECONFIG="$SCRIPT_DIR/kubeconfig"

echo "==> waiting for the node to report Ready..."
until kubectl get nodes 2>/dev/null | grep -q Ready; do
  sleep 5
done
kubectl get nodes

echo "==> building images locally"
( cd "$REPO_ROOT" && docker compose build api worker )

echo "==> shipping images to the instance (this is the slow step — the worker image is ~3.4GB, mostly Chromium)"
docker save daraz-price-tracker-api:latest    | gzip | ssh "${SSH_OPTS[@]}" "ubuntu@${PUBLIC_IP}" "gunzip | sudo k3s ctr images import -"
docker save daraz-price-tracker-worker:latest | gzip | ssh "${SSH_OPTS[@]}" "ubuntu@${PUBLIC_IP}" "gunzip | sudo k3s ctr images import -"

echo "==> applying k8s/ (same order as k8s/README.md's minikube walkthrough — k3s is a conformant cluster, nothing here is cloud-specific)"
kubectl apply -f "$REPO_ROOT/k8s/namespace.yaml"
kubectl apply -f "$REPO_ROOT/k8s/rbac.yaml"
kubectl apply -f "$REPO_ROOT/k8s/configmap.yaml"
kubectl apply -f "$REPO_ROOT/k8s/secret.yaml"
kubectl apply -f "$REPO_ROOT/k8s/postgres-statefulset.yaml"
kubectl apply -f "$REPO_ROOT/k8s/redis-statefulset.yaml"

echo "==> waiting for postgres..."
kubectl wait --for=condition=ready pod -l app=postgres -n price-tracker --timeout=180s

kubectl apply -f "$REPO_ROOT/k8s/migrate-job.yaml"
echo "==> waiting for migrations..."
kubectl wait --for=condition=complete job/migrate -n price-tracker --timeout=180s

kubectl apply -f "$REPO_ROOT/k8s/api-deployment.yaml"
kubectl apply -f "$REPO_ROOT/k8s/worker-deployment.yaml"

echo "==> monitoring stack (Prometheus + Grafana only — see note below on what's skipped)"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/namespace.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/prometheus-rbac.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/prometheus-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/prometheus-deployment.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/grafana-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/grafana-secret.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/grafana-dashboards.yaml"
kubectl apply -f "$REPO_ROOT/k8s/monitoring/grafana-deployment.yaml"

# prometheus-adapter and worker-hpa.yaml are deliberately NOT applied
# here. Steady-state memory *requests* for the full stack at the
# committed replica counts (2x api, 2x worker, postgres, redis,
# prometheus, grafana) already add up to ~1.9GiB — on a t3.small's 2GiB
# total, before k3s's own control-plane/containerd/Traefik overhead
# (typically 300-600MB), that doesn't fit. Scaling api/worker to 1
# replica each (below) is what makes the rest fit; the HPA's whole
# purpose is scaling worker up to 10 replicas under load, which this
# single 2GiB node can't honor regardless of whether the metrics path is
# wired up correctly — deploying it here would just demonstrate a
# HorizontalPodAutoscaler stuck at Pending pods, not autoscaling. That
# feature is demonstrated properly on minikube instead (see
# k8s/README.md's Autoscaling section), where the node actually has room
# to scale into.
echo "==> skipping prometheus-adapter + worker-hpa (see comment in this script — t3.small doesn't have the RAM for what they'd demonstrate)"

echo "==> fitting the workload to a t3.small (2GiB RAM): scaling api/worker to 1 replica each"
kubectl scale deployment/api -n price-tracker --replicas=1
kubectl scale deployment/worker -n price-tracker --replicas=1

echo "==> applying cloud demo access (Ingress for api, NodePort Services for Grafana/Prometheus)"
kubectl apply -f "$SCRIPT_DIR/manifests/cloud-access.yaml"

echo "==> waiting for the api Deployment to be ready..."
kubectl rollout status deployment/api -n price-tracker --timeout=180s

cat <<EOF

==================================================================
Deployed. URLs:

  API         http://${PUBLIC_IP}/
  Grafana     http://${PUBLIC_IP}:30030   (admin / see k8s/monitoring/grafana-secret.yaml)
  Prometheus  http://${PUBLIC_IP}:30090

  kubectl (local):  export KUBECONFIG=${SCRIPT_DIR}/kubeconfig
  SSH:              ssh -i ${SSH_KEY} ubuntu@${PUBLIC_IP}

Remember this instance is billing by the hour the whole time it exists —
see terraform/COSTS.md. When you're done:

  cd ${SCRIPT_DIR} && terraform destroy
==================================================================
EOF
