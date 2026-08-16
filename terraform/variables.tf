variable "aws_region" {
  description = "AWS region to deploy into. This account has other regions locked, so eu-north-1 is effectively the only valid value right now, but it's still a variable rather than hardcoded — the region shows up in several resource configs below and a variable keeps them from drifting out of sync."
  type        = string
  default     = "eu-north-1"
}

variable "my_ip" {
  description = <<-EOT
    Your current public IP, as x.x.x.x (no /32 suffix — that's added where
    it's used). Used to lock down SSH (and the k3s API port) to just you
    instead of the whole internet. No default on purpose: auto-detecting
    "your IP" from inside Terraform isn't reliable (this machine resolved
    two different addresses from two different "what's my IP" services in
    the same minute, almost certainly a multi-egress-IP NAT/ISP setup) and
    it changes over time anyway (home/mobile connections routinely get a
    new one). Get the real value yourself before running plan/apply:

        curl -4 ifconfig.me

    and put it in terraform.tfvars. If it changes later (new coffee shop,
    ISP reassigns your IP, ...), update terraform.tfvars and re-apply —
    the security group rule updates in place, no other resource is
    affected.
  EOT
  type        = string
}

variable "ssh_public_key_path" {
  description = "Path to your SSH public key, uploaded to AWS as the key pair for the instance. Defaults to the standard ed25519 location; ~ is expanded via pathexpand()."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "instance_type" {
  description = "EC2 instance type. t3.small (2 vCPU, 2GiB RAM) is the deliberate choice for this demo — see terraform/COSTS.md for the monthly cost and terraform/README.md for the memory-budget math behind why the cloud deploy runs 1 replica of api/worker instead of the 2 the local k8s/ manifests default to."
  type        = string
  default     = "t3.small"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GB. 20GB covers the OS, k3s, containerd's image store (including the ~3.4GB Playwright/Chromium worker image), and Postgres/Redis/Prometheus/Grafana's PVCs (all backed by k3s's local-path-provisioner on this same volume, since there's no EBS CSI driver or separate data volume here)."
  type        = number
  default     = 20
}
