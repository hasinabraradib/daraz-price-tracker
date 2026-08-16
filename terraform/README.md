# Terraform — AWS demo deploy

Provisions one EC2 instance running k3s (single-node Kubernetes) in
eu-north-1 and deploys the same `k8s/` manifests used for the local
minikube walkthrough. Built for cost-minimized, throwaway portfolio
demos, not production — read `terraform/COSTS.md` before applying
anything, and read this whole section before applying anything too.

## ⚠️ Billing alarm — read this first

This account already has an AWS Budget configured (`daraz-tracker-alarm`,
$1.00/month, confirmed via `aws budgets describe-budgets` while writing
this) that will alert on spend against a near-zero threshold — that's a
safety net, not a substitute for actually running `terraform destroy`
when you're done. A budget alert tells you money is being spent; it
doesn't stop the spending or tear anything down by itself. Concretely:

- **Everything this creates bills by the hour it exists**, not by usage
  — see `terraform/COSTS.md` for the exact rates (pulled live from the
  AWS Pricing API, not estimated).
- **Set a reminder** for whenever you finish a demo session. It's one
  command:
  ```bash
  cd terraform && terraform destroy
  ```
- **After destroying, verify in the AWS Console** (EC2 → Instances, EC2
  → Elastic IPs) that nothing is left running. `terraform destroy`
  removing everything from *state* and AWS actually having removed
  everything should be the same thing, but a 30-second look in the
  console costs nothing and confirms it.
- If you ever apply this and then lose the local `terraform.tfstate`
  file before destroying (laptop dies, directory gets deleted, ...), the
  resources it was tracking don't get identified/destroyed automatically
  by anything — see the comment in `terraform/versions.tf` for why local
  state means this is a real, not theoretical, risk for a setup like
  this one. If that happens, the AWS Console is the fallback: find and
  terminate the EC2 instance and release the Elastic IP by hand.

## Prerequisites

- Terraform >= 1.5 (`terraform version`)
- AWS CLI configured with credentials for this account
  (`aws sts get-caller-identity` should succeed)
- An SSH key at `~/.ssh/id_ed25519` / `~/.ssh/id_ed25519.pub` (or point
  `ssh_public_key_path` in `terraform.tfvars` at a different one)
- Docker running locally (`deploy.sh` builds the api/worker images on
  your machine and ships them to the instance — see that script's
  comments for why, given the instance's own 2GiB of RAM)
- Your current public IP:
  ```bash
  curl -4 ifconfig.me
  ```
  Two different "what's my IP" services returned two different answers
  when checked from the machine this was written on (multi-egress-IP
  NAT/ISP setup) — there's no reliable way to auto-detect this from
  inside Terraform, which is why `my_ip` has no default. Get the real
  value yourself and put it in `terraform.tfvars`.

## Setup

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: set my_ip to the value from `curl -4 ifconfig.me`
terraform init
```

## Review before applying — this is the important part

```bash
terraform validate
terraform plan
```

**Read the plan output resource by resource before running `apply`.**
Every resource it proposes creating is listed with its cost implication
in `terraform/COSTS.md` — cross-reference the two. Nothing in this repo
runs `terraform apply` for you; that's a deliberate line between "code
that's been written and reviewed" and "money being spent," and it's
meant to stay a manual, deliberate step every time.

## Apply and deploy

```bash
terraform apply
./deploy.sh
```

`deploy.sh` waits for SSH, fetches a working kubeconfig (rewritten to
point at the instance's public IP — see the `--tls-san` comment in
`user_data.sh.tpl` for why the default one k3s writes wouldn't work
remotely), builds and ships the api/worker images, applies `k8s/` in
dependency order, scales api/worker to 1 replica each (a t3.small's
2GiB doesn't fit the 2-replica defaults `k8s/` uses for minikube — see
the comment in `deploy.sh`), applies the cloud-specific
Ingress/NodePort access layer, and prints the URLs.

Terraform's own outputs are also available any time:

```bash
terraform output
terraform output -raw public_ip
```

## What's deliberately not deployed here

`prometheus-adapter` and `k8s/worker-hpa.yaml` are skipped by
`deploy.sh` — autoscaling worker up to 10 replicas isn't something a
single 2GiB node can honor regardless of whether the metrics plumbing is
correct, so deploying the HPA here would just demonstrate pods stuck
`Pending`, not autoscaling. That feature is demonstrated properly on
minikube instead — see `k8s/README.md`'s **Autoscaling** section, where
the node actually has room to scale into.

## Tear down

```bash
cd terraform
terraform destroy
```

Confirm it. Then actually go check the AWS Console per the billing
alarm section above — a habit worth having regardless of what any
tooling claims succeeded.

## Updating `my_ip`

If your IP changes (new network, ISP reassignment, ...) and SSH or
`kubectl` stops connecting:

```bash
# edit terraform.tfvars with the new my_ip
terraform apply
```

Only the security group rule changes — nothing else gets touched or
recreated.
