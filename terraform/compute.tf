# Looked up by attributes (owner + name pattern), not a hardcoded AMI ID.
# AMI IDs are region-specific (the same Ubuntu release has a different ID
# in every region) and they rot over time as Canonical publishes new
# builds with security patches under a new ID — a hardcoded ID either
# breaks the moment someone applies this in a different region, or
# quietly launches an increasingly-out-of-date, unpatched image forever.
# most_recent + the official Canonical owner ID (099720109477, not
# something to trust from a random AMI name search) gets the current
# Ubuntu 22.04 LTS build for whatever region var.aws_region resolves to.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "this" {
  key_name   = "daraz-price-tracker-demo"
  public_key = file(pathexpand(var.ssh_public_key_path))

  tags = { Name = "daraz-price-tracker-demo" }
}

# Allocated as its own resource (not the shorthand `instance = ...`
# argument directly on aws_eip) specifically so its address is known
# *before* the instance exists — user_data below needs the EIP's actual
# value baked in at boot time, to tell k3s "also trust API requests
# claiming to be this address" (--tls-san). k3s's self-signed cert only
# covers the instance's own view of itself (localhost, its private IP);
# it has no way to know its own Elastic IP unless told, and no work is
# needed to make them line up. aws_eip_association (bottom of this file)
# does the actual attach, once both this and the instance exist.
resource "aws_eip" "this" {
  domain = "vpc"

  tags = { Name = "daraz-price-tracker-demo" }
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.this.id]
  key_name               = aws_key_pair.this.key_name

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb
  }

  # Renders user_data.sh.tpl with the EIP's address already known (see
  # the comment on aws_eip.this above) — this is what lets k3s issue
  # itself a serving cert that's actually valid for the address you'll
  # connect to from outside AWS's network.
  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    public_ip = aws_eip.this.public_ip
  })

  tags = { Name = "daraz-price-tracker-demo" }
}

# Separate from the instance so Terraform can create/replace either one
# independently without forcing the other to be recreated too. An EIP
# costs nothing extra while it's attached to a running instance — AWS
# only starts billing for it (a small hourly charge) the moment it's
# *not* attached to anything running, which is exactly the state you'd
# accidentally leave one in by, say, terminating the instance by hand
# outside Terraform instead of running `terraform destroy` for the whole
# stack. `terraform destroy` releases this cleanly; nothing dangles.
resource "aws_eip_association" "this" {
  instance_id   = aws_instance.this.id
  allocation_id = aws_eip.this.id
}
