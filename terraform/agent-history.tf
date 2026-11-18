# S3 access for the SRE agent's history replica (bucket from terraform/history).
#
# Scope: read/write under sre-agent/<cluster>/ in that one bucket, nothing else.
# Default: EKS Pod Identity, which needs no IAM OIDC provider (blocked by the SCP in
# this account), and gives the credentials to the sre-agent ServiceAccount only.
# Fallback (agent_history_access = "node_role"): the policy goes on the node role,
# so every pod on the nodes could use it - acceptable in the lab, not in production.
# Both need EC2 nodes: the agent keeps its database on an EBS volume.

locals {
  history        = var.agent_history_bucket != "" && local.ec2
  history_pod_id = local.history && var.agent_history_access == "pod_identity"
  history_node   = local.history && var.agent_history_access == "node_role"
  history_prefix = "sre-agent/${var.cluster_name}"
  history_bucket = "arn:aws:s3:::${var.agent_history_bucket}"
}

data "aws_iam_policy_document" "agent_history" {
  count = local.history ? 1 : 0

  statement {
    sid       = "ListOwnPrefix"
    actions   = ["s3:ListBucket"]
    resources = [local.history_bucket]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [local.history_prefix, "${local.history_prefix}/*"]
    }
  }

  statement {
    sid       = "ReadWriteOwnPrefix"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${local.history_bucket}/${local.history_prefix}/*"]
  }
}

resource "aws_iam_policy" "agent_history" {
  count       = local.history ? 1 : 0
  name        = "${var.cluster_name}-sre-agent-history"
  description = "SRE agent: replicate its SQLite history to s3://${var.agent_history_bucket}/${local.history_prefix}/"
  policy      = data.aws_iam_policy_document.agent_history[0].json
}

# --- Pod Identity (default) ---------------------------------------------------
resource "aws_eks_addon" "pod_identity" {
  count = local.history_pod_id ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.workers] # a DaemonSet: runs on the EC2 nodes
}

data "aws_iam_policy_document" "pod_identity_assume" {
  count = local.history_pod_id ? 1 : 0

  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition { # only this account's EKS clusters...
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition { # ...and only this cluster; the association below pins namespace + ServiceAccount
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_eks_cluster.this.arn]
    }
  }
}

resource "aws_iam_role" "agent_history" {
  count              = local.history_pod_id ? 1 : 0
  name               = "${var.cluster_name}-sre-agent-history"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume[0].json
}

resource "aws_iam_role_policy_attachment" "agent_history" {
  count      = local.history_pod_id ? 1 : 0
  role       = aws_iam_role.agent_history[0].name
  policy_arn = aws_iam_policy.agent_history[0].arn
}

resource "aws_eks_pod_identity_association" "agent_history" {
  count = local.history_pod_id ? 1 : 0

  cluster_name    = aws_eks_cluster.this.name
  namespace       = "sre-agent"
  service_account = "sre-agent" # need not exist yet; created by make sre-agent-up
  role_arn        = aws_iam_role.agent_history[0].arn

  depends_on = [aws_eks_addon.pod_identity]
}

# --- Node role (fallback) -----------------------------------------------------
resource "aws_iam_role_policy_attachment" "node_agent_history" {
  count      = local.history_node ? 1 : 0
  role       = aws_iam_role.node.name
  policy_arn = aws_iam_policy.agent_history[0].arn
}
