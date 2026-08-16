terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state (the default backend — no `backend` block needed to get
  # it). Terraform's state file is the only record mapping the resources
  # declared in this code to the real objects AWS actually created (IDs,
  # ARNs, current attributes) — Terraform has no other way to know an EC2
  # instance "belongs" to this config short of that file. Lose it (delete
  # terraform.tfstate, or run `terraform apply` from a different machine
  # without copying it over) and Terraform no longer knows those
  # resources exist: `terraform plan` would propose creating everything
  # again from scratch, while the original VPC/instance/EIP keep running
  # and billing, now orphaned — nothing left pointing at them to `destroy`.
  # For a single-operator portfolio demo applied from one laptop, local
  # state is fine as long as that's understood. A team setup would use a
  # remote backend instead — typically S3 for the state file itself plus
  # a DynamoDB table for locking (so two people running `apply`
  # simultaneously don't corrupt the state) — neither of which this repo
  # sets up, since a single demo instance from a single machine has no
  # concurrent-writer problem to solve.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}
