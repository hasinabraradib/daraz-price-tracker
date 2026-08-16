data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "daraz-price-tracker-demo" }
}

# ONE public subnet — no private subnet. A private subnet's entire reason
# to exist is hosting things that shouldn't have a public IP but still
# need outbound internet access (apt/pip/docker pulls) via a NAT gateway.
# There's nothing here that needs that isolation: it's one instance
# running the whole stack, and it's meant to be reachable (that's the
# point of a demo). A NAT gateway is ~$32/month — a flat hourly charge
# for the gateway itself, on top of per-GB data processing charges — and
# it is, by a wide margin, the single most common accidental cost in AWS
# setups like this one: many Terraform tutorials/modules create a private
# subnet "for security" by default and a NAT gateway to go with it,
# without the actual workload ever needing either. Skipping both entirely
# here isn't a shortcut being cut for this demo, it's the correct
# architecture for what this demo actually is.
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "daraz-price-tracker-demo-public" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "daraz-price-tracker-demo" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "daraz-price-tracker-demo-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "this" {
  name        = "daraz-price-tracker-demo"
  description = "Demo instance: SSH+k3s API from my_ip only, HTTP and NodePort range open to the world for the actual demo"
  vpc_id      = aws_vpc.this.id

  # SSH restricted to my_ip/32, not 0.0.0.0/0. Port 22 open to the entire
  # internet is one of the first things automated scanners probe for on
  # any new AWS IP range — leaving it open trades a trivial convenience
  # (not having to update terraform.tfvars if your IP changes) for
  # continuous brute-force/credential-stuffing attempts against this box
  # for as long as it exists. A single-purpose demo box has no need to
  # accept that risk for literally zero benefit.
  ingress {
    description = "SSH from my_ip only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${var.my_ip}/32"]
  }

  # k3s's API server. Also locked to my_ip — this is what makes the
  # `kubeconfig fetch command` output usable for real remote kubectl
  # access from your own machine, rather than a file that just times out.
  # Not opened to 0.0.0.0/0 for the same reason SSH isn't: cluster-admin
  # access to this box is not something the whole internet needs.
  ingress {
    description = "k3s API server from my_ip only"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = ["${var.my_ip}/32"]
  }

  # HTTP, open to everyone — the actual point of a portfolio demo is
  # being able to send someone a link without them needing to be on an
  # allowlist. Served by k3s's bundled Traefik ingress
  # (terraform/manifests/cloud-access.yaml), fronting the api Service.
  ingress {
    description = "HTTP (api, via Traefik ingress)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Kubernetes' default NodePort range. Grafana and Prometheus are
  # exposed as NodePort Services (terraform/manifests/cloud-access.yaml)
  # rather than sharing port 80 through the ingress — they're
  # secondary/admin-facing tools for this demo, and a raw IP:port is a
  # fine way to reach them without adding path-based routing complexity
  # to the ingress for tools that aren't the actual product.
  ingress {
    description = "NodePort range (Grafana, Prometheus)"
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "daraz-price-tracker-demo" }
}
