terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Separate state from ../ on purpose: `make tf-down` destroys the cluster stack,
  # and the SRE agent's history must survive that. Local state, like the main stack.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "bank-sim"
      managed-by = "terraform"
      stack      = "sre-agent-history"
    }
  }
}
