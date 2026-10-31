# EKS control plane, add-ons, and either one managed node group (compute = "ec2")
# or Fargate profiles (compute = "fargate").
#
# Order matters (Terraform follows the depends_on below):
#   control plane -> vpc-cni + kube-proxy add-ons -> nodes or Fargate profiles -> coredns + metrics-server
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

locals {
  ec2     = var.compute == "ec2"
  fargate = var.compute == "fargate"
}

# Nodes: Amazon Linux 2023 EKS AMI. The launch template only adds what the
# managed node group cannot set itself: max pods, a gp3 root disk and IMDSv2.
resource "aws_launch_template" "nodes" {
  count = local.ec2 ? 1 : 0

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
  count = local.ec2 ? 1 : 0

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
    id      = aws_launch_template.nodes[0].id
    version = aws_launch_template.nodes[0].latest_version
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

# The resources were renamed when they got a count; keep existing clusters in place.
moved {
  from = aws_launch_template.nodes
  to   = aws_launch_template.nodes[0]
}

moved {
  from = aws_eks_node_group.workers
  to   = aws_eks_node_group.workers[0]
}

# Fargate: pods in matching namespaces run in private subnets, one micro-VM each.
# EKS creates one profile at a time, hence the chain. A pod is placed when it is
# created, so the profiles must exist before CoreDNS and the workloads.
resource "aws_eks_fargate_profile" "system" {
  count = local.fargate ? 1 : 0

  cluster_name           = aws_eks_cluster.this.name
  fargate_profile_name   = "system"
  pod_execution_role_arn = aws_iam_role.fargate[0].arn
  subnet_ids             = aws_subnet.private[*].id

  selector {
    namespace = "kube-system" # CoreDNS and metrics-server
  }

  depends_on = [aws_iam_role_policy_attachment.fargate, aws_route.private_nat]
}

resource "aws_eks_fargate_profile" "workloads" {
  count = local.fargate ? 1 : 0

  cluster_name           = aws_eks_cluster.this.name
  fargate_profile_name   = "workloads"
  pod_execution_role_arn = aws_iam_role.fargate[0].arn
  subnet_ids             = aws_subnet.private[*].id

  dynamic "selector" {
    for_each = var.fargate_namespaces
    content {
      namespace = selector.value
    }
  }

  depends_on = [aws_eks_fargate_profile.system]
}

resource "aws_eks_addon" "coredns" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "coredns"
  # without this, CoreDNS pods ask for EC2 nodes and stay Pending on Fargate
  configuration_values        = jsonencode(local.fargate ? { computeType = "Fargate" } : {})
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers, aws_eks_fargate_profile.system] # needs somewhere to run
}

resource "aws_eks_addon" "metrics_server" {
  count = var.enable_metrics_server ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "metrics-server"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers, aws_eks_fargate_profile.system]
}

# EBS volumes attach to EC2 instances, so there is no EBS on Fargate.
resource "aws_eks_addon" "ebs_csi" {
  count = var.enable_ebs_csi_driver && local.ec2 ? 1 : 0

  cluster_name = aws_eks_cluster.this.name
  addon_name   = "aws-ebs-csi-driver"
  configuration_values = jsonencode({
    defaultStorageClass = { enabled = true } # creates "ebs-csi-default-sc" (gp3) as the cluster default
  })
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers, aws_iam_role_policy_attachment.node_ebs_csi]
}

# Extra cluster admins, e.g. your console identity, so the EKS console's
# Resources tab works when terraform runs from the jump host under another identity.
resource "aws_eks_access_entry" "admins" {
  # the identity running terraform already has an entry (bootstrap_cluster_creator_admin_permissions)
  for_each = toset([for arn in var.admin_principal_arns : arn if arn != data.aws_caller_identity.current.arn])

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
}

resource "aws_eks_access_policy_association" "admins" {
  for_each = aws_eks_access_entry.admins

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}
