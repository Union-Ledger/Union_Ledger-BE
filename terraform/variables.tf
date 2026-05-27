variable "aws_region" {
  description = "AWS region (e.g. ap-northeast-2 for Seoul)"
  type        = string
  default     = "ap-northeast-2"
}

variable "project_name" {
  description = "Resource name prefix"
  type        = string
  default     = "union-ledger"
}

variable "instance_type" {
  description = "EC2 instance type (OCR-heavy workloads: t3.medium or larger)"
  type        = string
  default     = "t3.small"
}

variable "key_name" {
  description = "Existing EC2 key pair name in this region (required for SSH)"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH (use your public IP/32, e.g. 1.2.3.4/32)"
  type        = string
}

variable "allowed_api_cidr" {
  description = "CIDR allowed to reach the API on port 8000 (ignored if expose_api_port=false)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "allowed_web_cidr" {
  description = "CIDR allowed for HTTP/HTTPS (nginx + Let's Encrypt) on ports 80 and 443"
  type        = string
  default     = "0.0.0.0/0"
}

variable "expose_web_ports" {
  description = "Open TCP 80 and 443 for nginx and certbot"
  type        = bool
  default     = true
}

variable "expose_api_port" {
  description = "Open TCP 8000 on the security group. Set false when API is only reachable via nginx on 443."
  type        = bool
  default     = true
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 10
}

variable "app_install_dir" {
  description = "Directory on the instance where deploy.sh syncs the app"
  type        = string
  default     = "/opt/union-ledger"
}
