# AWS setup

Creates a private, encrypted, versioned S3 bucket for the vault, a KMS key, a least-privilege IAM user for the server, and CloudTrail auditing.

## 1. Apply Terraform
```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # git-ignored
aws sts get-caller-identity                    # put this ARN in admin_principal_arns
terraform init
terraform plan
terraform apply
```
> ⚠️ `admin_principal_arns` **must** include the identity you run Terraform as. The bucket policy denies every other principal, including other admins in the account.

Keep Terraform state in a separate, private, encrypted backend. Add a git-ignored `backend.tf`, for example an S3 backend in a different bucket.

## 2. Create the server's access key, outside Terraform
```bash
aws iam create-access-key --user-name second-brain-server
```
Put `AccessKeyId` and `SecretAccessKey` straight into Railway variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`). Don't save them anywhere else. Rotate them every 90 days.

## 3. (Recommended) MFA Delete
MFA Delete can only be turned on by the account **root** user, with the CLI:
```bash
aws s3api put-bucket-versioning --bucket <bucket> \
  --versioning-configuration Status=Enabled,MFADelete=Enabled \
  --mfa "arn:aws:iam::<account>:mfa/root-account-mfa-device <code>"
```

## 4. Upload an existing vault
```bash
aws s3 sync ~/path/to/vault s3://<bucket>/ --exclude ".git/*" --exclude ".obsidian/*" --exclude ".DS_Store"
```

## 5. Alerts (recommended)
Create an EventBridge rule or a CloudWatch metric filter on the CloudTrail data events that alerts on any principal other than `second-brain-server` or your admin role.
