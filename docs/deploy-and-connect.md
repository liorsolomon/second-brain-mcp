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

Optional, for persistent logins (see [Keep logins across redeploys](#keep-logins-across-redeploys)):

| Variable | Value |
|---|---|
| `SB_OAUTH_STORAGE_DIR` | the volume mount path, e.g. `/data/oauth` |
| `SB_OAUTH_STORAGE_KEY` | optional; encrypts the OAuth state. Defaults to a key derived from `SB_JWT_SIGNING_KEY` |

Check that the deploy is up: `curl https://<domain>/health` should return `{"status":"ok"}`, and any request to `/mcp` without a login should get a 401.

### Keep logins across redeploys
The OAuth proxy keeps registered clients and tokens on disk. By default that disk is the container's own filesystem, so every redeploy wipes it and each client (Claude Code, Claude.ai) has to go through consent and Google sign-in again. To keep that state, put it on a Railway volume:

1. In the Railway service, **right-click the canvas → Volume**, attach it to `second-brain-mcp`, and set the mount path to `/data`.
2. Set `SB_OAUTH_STORAGE_DIR=/data/oauth`.
3. Redeploy and sign in once from each client.

Railway mounts volumes owned by root. The image's entrypoint starts as root only to create `SB_OAUTH_STORAGE_DIR` and hand it to the unprivileged `brain` user (mode 700), then drops to `brain` with `setpriv --no-new-privs` before starting the server, so the server never runs as root. **Don't set `RAILWAY_RUN_UID`.** If the directory still isn't writable, startup fails with `SB_OAUTH_STORAGE_DIR is not writable` rather than silently falling back.

The state is encrypted at rest with Fernet. The key comes from `SB_OAUTH_STORAGE_KEY` if it's set, otherwise it's derived from `SB_JWT_SIGNING_KEY`. If you change that key, or `SB_JWT_SIGNING_KEY`, the old entries can no longer be read. They're ignored rather than causing errors, and every client signs in once more. Changing `SB_JWT_SIGNING_KEY` also invalidates issued tokens.

Notes:
- A service with a volume can only run one replica, and Railway stops the old deployment before it starts the new one, so expect a few seconds of downtime on each deploy.
- The volume only holds OAuth client registrations and tokens. The vault itself stays in S3.

**Check that it works:** sign in from Claude Code (`/mcp`) and call a tool, such as `health`. Then trigger a redeploy (`railway redeploy`, or push a commit) and wait for `/health` to return ok. Call the tool again. It should work without a browser window opening. In the deploy logs, look for `OAuth state persisted under /data/oauth` at startup.

## 3. Connect Claude
- **Claude.ai** (web, desktop, mobile): Settings → Connectors → **Add custom connector** → URL `https://<domain>/mcp`. Sign in with your allowlisted Google account.
- **Claude Code** (any computer):
  ```bash
  claude mcp add --transport http --scope user second-brain https://<domain>/mcp
  ```
  Then run `/mcp` in Claude Code to sign in.

## 4. Obsidian on your laptops and phone
Sync Obsidian with the same bucket using the **Remotely Save** plugin and the dedicated `second-brain-obsidian` IAM user. See [obsidian-sync.md](obsidian-sync.md).

## Running locally (stdio)
```bash
SB_VAULT_PATH=~/path/to/vault uv run second-brain-mcp
claude mcp add second-brain -- uv --directory /path/to/second-brain-mcp run second-brain-mcp
```
