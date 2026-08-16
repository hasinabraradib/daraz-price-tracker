# ---------------------------------------------------------------------------
# What this deliberately does NOT provision, and why — read this before
# reaching for any of these on top of what's here. Every one of them is a
# real, common piece of "production-grade AWS" that would add ongoing cost
# for zero benefit to a single-node portfolio demo (see terraform/COSTS.md
# for the full breakdown of what IS provisioned):
#
#   EKS         ~$73/month for the control plane ALONE (a flat per-cluster
#               fee, before a single worker node), on top of the EC2
#               nodes you'd still need. k3s (installed on the one EC2
#               instance below, see user_data.sh.tpl) is a fully
#               conformant Kubernetes distribution — every manifest under
#               k8s/ applies to it completely unchanged. There is nothing
#               an EKS control plane would do here that this demo needs
#               and k3s doesn't already do for free.
#
#   RDS         A managed db.t3.micro Postgres instance runs ~$12-15/month
#               even in the free-tier-eligible shape (and the free tier
#               only covers the first 12 months on the account, not this
#               specific database). Postgres already runs as a normal pod
#               in-cluster (k8s/postgres-statefulset.yaml) with its own
#               PVC — one less AWS resource to provision, tag, and
#               eventually remember to destroy.
#
#   ALB         An Application Load Balancer is ~$16-20/month just for it
#               to exist (the per-hour charge applies regardless of
#               traffic), before its per-LCU usage cost. A single EC2
#               instance with an Elastic IP and k3s's bundled Traefik
#               ingress (listening on the instance's own port 80) serves
#               this demo's entire traffic just fine — there's no
#               multi-node fleet here for a load balancer to balance
#               across.
#
#   NAT gateway ~$32/month PLUS per-GB data processing charges, and it's
#               the single most common accidental cost in a setup like
#               this one — see the comment on the subnet in network.tf
#               for the full reasoning on why there's no private subnet
#               here for a NAT gateway to even serve.
#
# All four of these are the right call in a real production system with
# real traffic, real availability requirements, and a real team. None of
# that applies to a demo instance that gets `terraform destroy`'d after a
# few hours of showing someone a portfolio.
# ---------------------------------------------------------------------------

locals {
  tags = {
    Project     = "daraz-price-tracker"
    Environment = "demo"
  }
}
