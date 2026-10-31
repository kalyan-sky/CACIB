variable "region" {
  description = "AWS region (Singapore = ap-southeast-1, Sydney = ap-southeast-2)"
  type        = string
  default     = "ap-southeast-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "bank-sim"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Raise it one minor at a time to upgrade (control plane first, then nodes)."
  type        = string
  default     = "1.37"
}

variable "instance_type" {
  description = "Worker instance type. AWS Free plan accounts can only launch free-tier-eligible types (e.g. m7i-flex.large)."
  type        = string
  default     = "m7i-flex.large"
}

variable "node_count" {
  description = "Worker nodes, spread over the three availability zones"
  type        = number
  default     = 3
}

variable "vpc_cidr" {
  description = "VPC address range"
  type        = string
  default     = "10.20.0.0/16"
}

variable "enable_nat_gateway" {
  description = "Give the private subnets internet access through a NAT gateway (~USD 0.06/hour + data). Not needed with EC2 nodes, which run in public subnets; compute = \"fargate\" turns it on, because Fargate pods run in the private subnets."
  type        = bool
  default     = false
}

variable "api_public_cidrs" {
  description = "Who may reach the Kubernetes API endpoint. CloudShell addresses change, so the lab default is open. With a jump host, set it to the jump host's static IP only, e.g. [\"34.87.1.2/32\"]."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_metrics_server" {
  description = "Install metrics-server (kubectl top) as an EKS add-on"
  type        = bool
  default     = true
}

variable "enable_ebs_csi_driver" {
  description = "Install the EBS CSI driver (PersistentVolumes on EBS) with a default gp3 StorageClass"
  type        = bool
  default     = true
}

variable "compute" {
  description = "Where pods run: \"ec2\" = a managed node group, \"fargate\" = AWS Fargate (no nodes; each pod gets its own micro-VM in a private subnet)"
  type        = string
  default     = "ec2"

  validation {
    condition     = contains(["ec2", "fargate"], var.compute)
    error_message = "compute must be \"ec2\" or \"fargate\"."
  }
}

variable "fargate_namespaces" {
  description = "Fargate mode: namespaces whose pods run on Fargate, besides kube-system. Wildcards * and ? are allowed; \"c??-*\" matches the classic cases. At most 5."
  type        = list(string)
  default     = ["c??-*", "default"]

  validation {
    condition     = length(var.fargate_namespaces) >= 1 && length(var.fargate_namespaces) <= 5
    error_message = "A Fargate profile takes 1 to 5 namespace selectors."
  }
}

variable "admin_principal_arns" {
  description = "Extra IAM users or roles that get cluster-admin, e.g. the identity you use in the AWS console. Whoever runs terraform apply always has it. The root user can't be added."
  type        = list(string)
  default     = []
}
