# Cost breakdown — eu-north-1 (Stockholm)

Real, current on-demand pricing pulled from the AWS Pricing API for this
account/region while writing this (`aws pricing get-products`), not
estimated from memory. Prices change over time — re-run the same query if
this ever looks stale:

```bash
aws pricing get-products --region us-east-1 --service-code AmazonEC2 \
  --filters 'Type=TERM_MATCH,Field=instanceType,Value=t3.small' \
            'Type=TERM_MATCH,Field=regionCode,Value=eu-north-1' \
            'Type=TERM_MATCH,Field=operatingSystem,Value=Linux' \
            'Type=TERM_MATCH,Field=tenancy,Value=Shared' \
            'Type=TERM_MATCH,Field=preInstalledSw,Value=NA' \
            'Type=TERM_MATCH,Field=capacitystatus,Value=Used'
```

(The AWS Pricing API itself is only queryable via the `us-east-1`
endpoint regardless of which region's prices you're asking about — that's
normal, not a mistake in the command above.)

## Per-resource

| Resource | Rate | Monthly (× 730 hrs) |
|---|---|---|
| EC2 `t3.small` | $0.0216/hr | $15.77 |
| EBS `gp3`, 20GB | $0.0836/GB-month | $1.67 |
| Elastic IP (attached to a running instance) | **$0** | **$0** |
| Elastic IP (unattached, or attached to a *stopped* instance) | $0.005/hr flat AWS-wide rate | $3.65 |
| VPC, subnet, internet gateway, route table, security group, key pair | $0 | $0 |
| Data transfer out | first 100GB/month free (AWS account-wide Free Tier, not region-specific) | $0 for anything demo-scale |

**Total while the instance is running: ~$17.44/month**, or **~$0.024/hour**
— the EC2 instance and its EBS volume are the only two things on this
list that actually cost anything, and both only while `terraform apply`
has created them.

The Elastic IP row is listed twice on purpose: `aws_eip_association` in
`compute.tf` keeps it attached to the running instance for the entire
time the instance exists under normal use, so its steady-state cost here
is $0. The $0.005/hr rate only applies if the instance were terminated
by hand (outside Terraform) while the EIP allocation itself was left
behind, or the instance were stopped rather than terminated — both are
things a plain `terraform destroy` avoids entirely, since it removes the
association and releases the allocation together, in the right order.

## What a demo session actually costs

| Duration | Cost |
|---|---|
| 3-hour demo | **~$0.07** |
| Left running for a full day by accident | ~$0.58 |
| Left running for a full week by accident | ~$4.03 |
| Left running for a full month by accident | ~$17.44 |

Three hours of showing this to someone costs less than a coffee. A month
of forgetting to tear it down costs about as much as a cheap streaming
subscription — annoying, not catastrophic, but there's no reason to pay
it when a demo session is over.

## When you're done

```bash
cd terraform
terraform destroy
```

**Every resource this creates is either free or only bills while it
exists — there is nothing here that keeps costing money after
`terraform destroy` completes.** Confirm the destroy actually finished
(check the AWS Console → EC2 → Instances, and → Elastic IPs, both should
be empty) before considering a demo session actually closed out. See
`terraform/README.md`'s billing alarm section for a second line of
defense in case a `destroy` gets forgotten anyway.
