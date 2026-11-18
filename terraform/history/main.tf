# Durable history for the SRE agent: Litestream replicates the agent's SQLite database
# (incidents, RCAs, approvals, audit log, accuracy verdicts) here, and restores it when a
# new pod starts on a rebuilt cluster. One prefix per cluster: sre-agent/<cluster>/.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "history" {
  bucket        = "${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "history" {
  bucket = aws_s3_bucket.history.id
  rule {
    object_ownership = "BucketOwnerEnforced" # no ACLs
  }
}

resource "aws_s3_bucket_public_access_block" "history" {
  bucket                  = aws_s3_bucket.history.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "history" {
  bucket = aws_s3_bucket.history.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "history" {
  bucket = aws_s3_bucket.history.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: no KMS key cost in the lab; use aws:kms with a CMK in production
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "history" {
  bucket = aws_s3_bucket.history.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.history]
}

# TLS only: refuse any request that is not over HTTPS.
data "aws_iam_policy_document" "tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.history.arn,
      "${aws_s3_bucket.history.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "history" {
  bucket = aws_s3_bucket.history.id
  policy = data.aws_iam_policy_document.tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.history]
}
