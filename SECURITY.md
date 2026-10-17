# Security policy

This server guards someone's private notes, including notes about other people. Please treat security issues seriously and report them privately.

## Reporting a vulnerability
Use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability"). Please don't open a public issue. You'll get a response within 7 days.

## Security model (summary)
- **Login:** Google OAuth through FastMCP's OAuth proxy. On every MCP request, the token's verified email must be on `SB_ALLOWED_EMAILS`. An empty allowlist denies everyone. HTTP mode refuses to start without auth.
- **No hard deletes:** there's no delete tool; `archive` moves notes to `Archive/`. On S3, bucket versioning keeps every version, and the bucket policy stops the server from deleting versions.
- **Paths:** every path is normalized and checked to stay inside the vault. Hidden folders (`.git`, `.obsidian`) can't be reached.
- **S3** (see `infra/terraform`): all public access blocked; SSE-KMS with a customer-managed key; non-TLS requests refused; access denied to every principal except the server and named admins; CloudTrail data events on every read and write.
- **Server credentials:** a dedicated IAM user limited to one bucket and one key. Its access key is created outside Terraform, so it never appears in state.
- **Prompt injection:** note contents are returned as data. The server tells clients not to follow instructions found in notes, and no tool can destroy data.
- **Logs** record tool and method names, never note contents.

## Out of scope
- Anything a connected AI client does with notes it was allowed to read.
- Compromise of the Google, AWS, Railway or GitHub accounts behind a deployment. Protect them with hardware-key 2FA.
