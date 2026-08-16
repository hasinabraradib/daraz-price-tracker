#!/usr/bin/env bash
set -euxo pipefail

# Runs once, on first boot, as root (standard EC2 user_data behavior).
# Output lands in /var/log/cloud-init-output.log on the instance — check
# there first if kubectl/k3s/helm seem missing after the instance is up.

# k3s: a fully conformant single-binary Kubernetes distribution (API
# server, controller-manager, scheduler, containerd, CoreDNS, a
# ServiceLB/klipper-lb, and a Traefik ingress controller, all in one
# process). Every manifest under k8s/ — StatefulSets, Deployments, Jobs,
# RBAC, the aggregated custom.metrics.k8s.io APIService — applies to it
# completely unchanged; this is genuine upstream Kubernetes, not a
# lookalike. We are deliberately NOT using EKS: its control plane alone
# is a flat ~$73/month before a single worker node exists, for a
# single-node portfolio demo that gets zero benefit from a managed,
# multi-AZ control plane it will never come close to stressing.
#
# --tls-san ${public_ip}: k3s's self-signed serving cert only covers what
# it can see about itself (localhost, its private IP) — it has no way to
# know its own Elastic IP unless told, since the EIP is a NAT mapping AWS
# does outside the instance, not an address any network interface on the
# instance is ever actually configured with. Without this flag, kubectl
# from outside AWS (i.e. your own laptop, using the fetched kubeconfig)
# would fail TLS verification the moment it connected to the public IP
# instead of 127.0.0.1.
# --write-kubeconfig-mode 644: makes /etc/rancher/k3s/k3s.yaml
# world-readable, so it can be fetched over SSH as the ubuntu user
# without needing sudo on both ends.
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--tls-san ${public_ip} --write-kubeconfig-mode 644" sh -

# Standalone kubectl, matching whatever the current stable release is —
# k3s bundles its own (`k3s kubectl ...`), but installing the real binary
# means plain `kubectl` works too, matching every command in k8s/README.md.
KUBECTL_VERSION=$(curl -sL https://dl.k8s.io/release/stable.txt)
curl -sLo /usr/local/bin/kubectl "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl"
chmod +x /usr/local/bin/kubectl

# helm — nothing under k8s/ actually needs it (everything there is
# deliberately plain YAML, see k8s/README.md), installed anyway since
# it's genuinely useful for ad hoc inspection on the box itself.
curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# So kubectl/helm just work for whoever SSHes in, without extra setup.
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> /root/.bashrc
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> /home/ubuntu/.bashrc

# Block until k3s is actually answering, not just installed — deploy.sh
# starts trying to reach the API server shortly after `terraform apply`
# returns, and there's no reason to make it retry-loop around a
# still-booting control plane when this script can just wait here first.
until /usr/local/bin/kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml get nodes 2>/dev/null | grep -q Ready; do
  sleep 5
done
