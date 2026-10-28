# EKS control plane, add-ons and one managed node group.
#
# Order matters (Terraform follows the depends_on below):
#   control plane -> vpc-cni + kube-proxy add-ons -> nodes -> coredns + metrics-server
# vpc-cni must carry its settings (network policy enforcement, prefix
# delegation) before the first node joins, or the nodes start without them.

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  # Install vpc-cni / kube-proxy / coredns as managed add-ons below instead of
  # the unmanaged defaults, so their versions and settings live in code.
  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true # whoever runs terraform apply gets cluster-admin
  }

  # STANDARD avoids extended-support charges; an out-of-date version gets auto-upgraded instead.
  upgrade_policy {
    support_type = "STANDARD"
  }

  vpc_config {
    subnet_ids              = concat(aws_subnet.public[*].id, aws_subnet.private[*].id)
    endpoint_public_access  = true
    endpoint_private_access = true
    public_access_cidrs     = var.api_public_cidrs
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "vpc-cni"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true" # enforce Kubernetes NetworkPolicies (scenario 03)
    env = {
      ENABLE_PREFIX_DELEGATION = "true" # /28 prefixes per ENI: ~110 pods per node instead of ~29
      WARM_PREFIX_TARGET       = "1"
    }
  })
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
}

# nodeadm (the AL2023 node bootstrap) merges this NodeConfig with the cluster
# settings EKS supplies for managed node groups.
locals {
  node_user_data = <<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="BOUNDARY"

    --BOUNDARY
    Content-Type: application/node.eks.aws

    ---
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          maxPods: 110

    --BOUNDARY--
  EOT
}

# Nodes: Amazon Linux 2023 EKS AMI. The launch template only adds what the
# managed node group cannot set itself: max pods, a gp3 root disk and IMDSv2.
resource "aws_launch_template" "nodes" {
  name_prefix            = "${var.cluster_name}-nodes-"
  update_default_version = true

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2
  }

  user_data = base64encode(local.node_user_data)

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.cluster_name}-node" }
  }
}

resource "aws_eks_node_group" "workers" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "workers"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id
  version         = aws_eks_cluster.this.version # nodes follow the control plane on upgrades
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  instance_types  = [var.instance_type]

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_count + 1 # room for one surge node during upgrades
  }

  update_config {
    max_unavailable = 1 # replace one node at a time; PodDisruptionBudgets are honoured
  }

  launch_template {
    id      = aws_launch_template.nodes.id
    version = aws_launch_template.nodes.latest_version
  }

  labels = {
    "bank.sim/datacentre" = "aws-${var.region}"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
  ]
}

resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers] # needs nodes to become healthy
}

resource "aws_eks_addon" "metrics_server" {
  count = var.enable_metrics_server ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "metrics-server"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers]
}

resource "aws_eks_addon" "ebs_csi" {
  count = var.enable_ebs_csi_driver ? 1 : 0

  cluster_name = aws_eks_cluster.this.name
  addon_name   = "aws-ebs-csi-driver"
  configuration_values = jsonencode({
    defaultStorageClass = { enabled = true } # creates "ebs-csi-default-sc" (gp3) as the cluster default
  })
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers, aws_iam_role_policy_attachment.node_ebs_csi]
}
