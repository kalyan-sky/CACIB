output "bucket" {
  description = "Set agent_history_bucket to this in ../terraform.tfvars"
  value       = aws_s3_bucket.history.bucket
}

output "region" {
  value = var.region
}
