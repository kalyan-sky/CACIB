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
  description = "Give the private subnets internet access through a NAT gateway (~USD 0.05/hour + data). Not needed for the lab: nodes run in public subnets."
  type        = bool
  default     = false
}

variable "api_public_cidrs" {
  description = "Who may reach the Kubernetes API endpoint. CloudShell addresses change, so the lab default is open; restrict it in real life."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_metrics_server" {
  description = "Install metrics-server (kubectl top) as an EKS add-on"
  type        = bool
  default     = true
}
