output "bucket" {
  value = aws_s3_bucket.vault.id
}

output "kms_key_arn" {
  value = aws_kms_key.vault.arn
}

output "server_user" {
  value = aws_iam_user.server.name
}

output "next_step" {
  value = "aws iam create-access-key --user-name ${aws_iam_user.server.name}  # put the result in Railway secrets only"
}
