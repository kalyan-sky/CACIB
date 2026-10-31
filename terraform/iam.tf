# IAM roles for the control plane and the worker nodes. No IAM OIDC provider:
# nothing in the lab needs IRSA, and sandbox accounts often block it.

data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.cluster_name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.cluster_name}-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy", # VPC CNI uses the node role (no IRSA)
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore", # shell on a node via SSM, for debugging
  ])

  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# EBS CSI driver permissions on the node role (no OIDC / IRSA needed). The
# controller reaches them through the instance metadata service, which is why
# the launch template allows a hop limit of 2.
resource "aws_iam_role_policy_attachment" "node_ebs_csi" {
  count      = var.enable_ebs_csi_driver && var.compute == "ec2" ? 1 : 0
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# Fargate pod execution role: what Fargate itself uses to pull images and
# register its micro-VMs with the cluster (not what the pod's code runs as).
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "fargate_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks-fargate-pods.amazonaws.com"]
    }
    condition { # only Fargate profiles of this cluster may use it
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:fargateprofile/${var.cluster_name}/*"]
    }
  }
}

resource "aws_iam_role" "fargate" {
  count              = var.compute == "fargate" ? 1 : 0
  name               = "${var.cluster_name}-fargate-pods"
  assume_role_policy = data.aws_iam_policy_document.fargate_assume.json
}

resource "aws_iam_role_policy_attachment" "fargate" {
  count      = var.compute == "fargate" ? 1 : 0
  role       = aws_iam_role.fargate[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy"
}
