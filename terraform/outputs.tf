output "public_ip" {
  description = "The instance's Elastic IP"
  value       = aws_eip.this.public_ip
}

output "ssh_command" {
  description = "Ready-to-paste SSH command"
  value       = "ssh ubuntu@${aws_eip.this.public_ip}"
}

output "kubeconfig_fetch_command" {
  description = "Fetches the k3s kubeconfig and rewrites it to point at the public IP instead of 127.0.0.1 (what k3s writes by default, since it doesn't know its own Elastic IP — see the --tls-san comment in user_data.sh.tpl). Run from the terraform/ directory; deploy.sh does this same thing automatically."
  value       = "ssh ubuntu@${aws_eip.this.public_ip} cat /etc/rancher/k3s/k3s.yaml | sed 's/127.0.0.1/${aws_eip.this.public_ip}/' > kubeconfig && export KUBECONFIG=$(pwd)/kubeconfig"
}

output "demo_urls" {
  description = "Where each service will be reachable once k8s/ is deployed (see deploy.sh) — not live until deploy.sh has actually run apply against the cluster."
  value = {
    api        = "http://${aws_eip.this.public_ip}/"
    grafana    = "http://${aws_eip.this.public_ip}:30030"
    prometheus = "http://${aws_eip.this.public_ip}:30090"
  }
}
