terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is kept locally in this folder (terraform.tfstate) - in CloudShell
  # that is your persistent home directory. For a team, move it to a remote
  # backend with locking, e.g. S3 with `use_lockfile = true`, or GitLab's
  # built-in Terraform state (see docs/terraform-gitlab-argocd.md).
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "bank-sim"
      managed-by = "terraform"
    }
  }
}
