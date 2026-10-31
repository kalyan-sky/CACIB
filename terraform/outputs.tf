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
