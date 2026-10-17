variable "region" {
  description = "AWS region for the vault bucket"
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique name for the vault bucket (an audit bucket named <bucket>-audit is also created)"
  type        = string
}

variable "prefix" {
  description = "Optional key prefix inside the bucket (e.g. \"vault/\")"
  type        = string
  default     = ""
}

variable "admin_principal_arns" {
  description = <<-EOT
    IAM principals (besides the server) allowed into the bucket and allowed to delete versions.
    MUST include the principal you run Terraform as, or you will lock yourself out of the bucket
    (only the account root could then fix the policy). Wildcards allowed, e.g. an SSO role:
    "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/*AdministratorAccess*".
  EOT
  type        = list(string)
  validation {
    condition     = length(var.admin_principal_arns) > 0
    error_message = "Set admin_principal_arns to at least the principal you run Terraform as."
  }
}
