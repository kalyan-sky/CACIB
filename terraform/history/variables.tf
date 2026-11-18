variable "region" {
  description = "AWS region of the bucket; use the cluster's region (no cross-region transfer)"
  type        = string
  default     = "ap-southeast-1"
}

variable "name_prefix" {
  description = "Bucket name prefix; the account ID and region are appended to make it globally unique"
  type        = string
  default     = "bank-sim-sre-history"
}

variable "noncurrent_version_days" {
  description = "Keep overwritten or deleted objects this many days (recovery from a bad write or a mistaken delete)"
  type        = number
  default     = 30
}

variable "force_destroy" {
  description = "Allow `terraform destroy` to delete a bucket that still holds history. Leave false except for a final clean-up."
  type        = bool
  default     = false
}
