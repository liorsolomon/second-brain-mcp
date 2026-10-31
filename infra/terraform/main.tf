terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }
  # Configure a remote, locked-down backend in backend.tf (git-ignored). Never commit state.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "second-brain-mcp" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  account_id    = data.aws_caller_identity.me.account_id
  bucket_arn    = "arn:aws:s3:::${var.bucket_name}"
  object_arn    = "arn:aws:s3:::${var.bucket_name}/${var.prefix}*"
  admin_arns    = concat(var.admin_principal_arns, ["arn:aws:iam::${local.account_id}:root"])
  server_arn    = aws_iam_user.server.arn
  obsidian_arns = var.obsidian_sync ? [aws_iam_user.obsidian[0].arn] : []
  allowed_arns  = concat(local.admin_arns, [local.server_arn], local.obsidian_arns)
}

# ---------------------------------------------------------------- KMS

resource "aws_kms_key" "vault" {
  description             = "second-brain vault encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "ServerUseViaS3Only"
        Effect    = "Allow"
        Principal = { AWS = local.server_arn }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_kms_alias" "vault" {
  name          = "alias/second-brain-vault"
  target_key_id = aws_kms_key.vault.key_id
}

# ---------------------------------------------------------------- Vault bucket

resource "aws_s3_bucket" "vault" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_ownership_controls" "vault" {
  bucket = aws_s3_bucket.vault.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "vault" {
  bucket                  = aws_s3_bucket.vault.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "vault" {
  bucket = aws_s3_bucket.vault.id
  versioning_configuration { status = "Enabled" }
  # MFA Delete can only be enabled by the root user via the CLI; see docs/aws-setup.md.
}

resource "aws_s3_bucket_server_side_encryption_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.vault.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "vault" {
  bucket     = aws_s3_bucket.vault.id
  depends_on = [aws_s3_bucket_public_access_block.vault]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyEveryoneElse"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { StringNotLike = { "aws:PrincipalArn" = local.allowed_arns } }
      },
      {
        Sid       = "DenyWrongKmsKey"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.bucket_arn}/*"
        Condition = {
          StringNotEqualsIfExists = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.vault.arn }
        }
      },
      {
        Sid       = "ProtectHistory"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObjectVersion", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration", "s3:DeleteBucket"]
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { StringNotLike = { "aws:PrincipalArn" = local.admin_arns } }
      },
    ]
  })
}

# ---------------------------------------------------------------- Server identity

resource "aws_iam_user" "server" {
  name = "second-brain-server"
}

resource "aws_iam_user_policy" "server" {
  name = "second-brain-vault-access"
  user = aws_iam_user.server.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListVault"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["${var.prefix}*"] } }
      },
      {
        Sid      = "ReadWriteNotes"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.object_arn
      },
      {
        Sid      = "UseVaultKey"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.vault.arn
      },
    ]
  })
}

# The access key is created outside Terraform (aws iam create-access-key) so the
# secret never lands in Terraform state. See docs/aws-setup.md.

# ---------------------------------------------------------------- Obsidian sync identity

# A separate user for the Remotely Save plugin on your devices. Same note access as the
# server, but its key lives on laptops and phones, so it can be revoked on its own.
# It cannot delete object versions (ProtectHistory in the bucket policy).
resource "aws_iam_user" "obsidian" {
  count = var.obsidian_sync ? 1 : 0
  name  = "second-brain-obsidian"
}

resource "aws_iam_user_policy" "obsidian" {
  count = var.obsidian_sync ? 1 : 0
  name  = "second-brain-obsidian-sync"
  user  = aws_iam_user.obsidian[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListVault"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = local.bucket_arn
      },
      {
        Sid      = "SyncNotes"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = local.object_arn
      },
      {
        Sid      = "UseVaultKey"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.vault.arn
      },
    ]
  })
}

# Obsidian mobile (and desktop without the plugin's CORS bypass) calls S3 from an app
# origin, so the bucket must allow those origins. CORS doesn't grant access: every
# request still has to be signed by an allowed IAM user.
resource "aws_s3_bucket_cors_configuration" "vault" {
  count  = var.obsidian_sync ? 1 : 0
  bucket = aws_s3_bucket.vault.id
  cors_rule {
    allowed_origins = ["app://obsidian.md", "capacitor://localhost", "http://localhost"]
    allowed_methods = ["GET", "PUT", "POST", "DELETE", "HEAD"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Last-Modified", "x-amz-version-id"]
    max_age_seconds = 3600
  }
}

# ---------------------------------------------------------------- Audit

resource "aws_s3_bucket" "audit" {
  bucket = "${var.bucket_name}-audit"
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket     = aws_s3_bucket.audit.id
  depends_on = [aws_s3_bucket_public_access_block.audit]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.audit.arn
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.audit.arn}/AWSLogs/${local.account_id}/*"
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

resource "aws_cloudtrail" "vault" {
  name                          = "second-brain-vault"
  s3_bucket_name                = aws_s3_bucket.audit.id
  include_global_service_events = false
  enable_log_file_validation    = true
  depends_on                    = [aws_s3_bucket_policy.audit]

  event_selector {
    read_write_type           = "All"
    include_management_events = false
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${local.bucket_arn}/"]
    }
  }
}
