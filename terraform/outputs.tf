output "region" {
  value = var.region
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "kubernetes_version" {
  value = aws_eks_cluster.this.version
}

output "compute" {
  description = "ec2 (managed node group) or fargate"
  value       = var.compute
}

output "api_allowed_cidrs" {
  description = "Who can reach the Kubernetes API (public endpoint)"
  value       = var.api_public_cidrs
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the cluster"
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.this.name} --region ${var.region} --alias eks-${aws_eks_cluster.this.name}"
}

output "agent_history_replica_url" {
  description = "Where the SRE agent replicates its history (empty: not configured, or compute is fargate)"
  value       = local.history ? "s3://${var.agent_history_bucket}/${local.history_prefix}" : ""
}

output "agent_history_access" {
  value = local.history ? var.agent_history_access : ""
}
