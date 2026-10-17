# Deploy to Railway and connect Claude

## 1. Google OAuth client
In Google Cloud Console, go to **APIs & Services → Credentials → Create OAuth client ID → Web application**.
- Authorized redirect URI: `https://<your-railway-domain>/auth/callback`
- Scopes used: `openid`, `email`

## 2. Railway
Create a service from this repo. It uses the `Dockerfile` and `railway.toml`. Set these variables:

| Variable | Value |
|---|---|
| `SB_BASE_URL` | `https://<your-railway-domain>` |
| `SB_GOOGLE_CLIENT_ID` / `SB_GOOGLE_CLIENT_SECRET` | from step 1 |
| `SB_JWT_SIGNING_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SB_ALLOWED_EMAILS` | your Google account email (comma-separated for more than one) |
| `SB_S3_BUCKET`, `SB_S3_REGION` | from Terraform |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | from `docs/aws-setup.md` step 2 |

Check that the deploy is up: `curl https://<domain>/health` should return `{"status":"ok"}`, and any request to `/mcp` without a login should get a 401.

## 3. Connect Claude
- **Claude.ai** (web, desktop, mobile): Settings → Connectors → **Add custom connector** → URL `https://<domain>/mcp`. Sign in with your allowlisted Google account.
- **Claude Code** (any computer):
  ```bash
  claude mcp add --transport http --scope user second-brain https://<domain>/mcp
  ```
  Then run `/mcp` in Claude Code to sign in.

## 4. Obsidian on your laptops
Install the **Remotely Save** plugin and choose S3. Use an IAM user or role scoped the same way as the server's, and leave the plugin's own end-to-end encryption **off**, because the server has to be able to read the notes.

## Running locally (stdio)
```bash
SB_VAULT_PATH=~/path/to/vault uv run second-brain-mcp
claude mcp add second-brain -- uv --directory /path/to/second-brain-mcp run second-brain-mcp
```
