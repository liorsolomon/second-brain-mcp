# Security policy

This server guards someone's private notes, including notes about other people. Please treat security issues seriously and report them privately.

## Reporting a vulnerability
Use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability"). Please don't open a public issue. You'll get a response within 7 days.

## Security model (summary)
- **Login:** Google OAuth through FastMCP's OAuth proxy. On every MCP request, including the initial handshake, the token's verified email must be on `SB_ALLOWED_EMAILS`. An empty allowlist denies everyone. HTTP mode refuses to start without auth. Only tokens the server issued itself are accepted; Google tokens issued to other apps can't be replayed.
- **Client registration:** open dynamic client registration (`POST /register`) is **off** by default. Claude.ai and Claude Code identify themselves with client ID metadata documents, so anonymous callers can't write registration records to OAuth storage. `SB_ALLOW_DYNAMIC_CLIENT_REGISTRATION=true` turns it back on for clients that need it, at the cost of unlimited, never-expiring registrations.
- **Consent:** each new client must be approved on a consent page that shows its identity and callback URL before Google sign-in.
- **No hard deletes:** there's no delete tool; `archive` moves notes to `Archive/`. On S3, bucket versioning keeps every version, and the bucket policy stops the server from deleting versions.
- **Paths:** every path is normalized and checked to stay inside the vault. Hidden folders (`.git`, `.obsidian`) can't be reached.
- **S3** (see `infra/terraform`): all public access blocked; SSE-KMS with a customer-managed key; non-TLS requests refused; access denied to every principal except the server, the optional Obsidian sync user and named admins; CloudTrail data events on every read and write.
- **Credentials:** the server and Obsidian sync each get their own IAM user, limited to one bucket and one key, so either can be revoked alone. Neither can delete object versions. Access keys are created outside Terraform, so they never appear in state.
- **Prompt injection:** note contents are returned as data. The server tells clients not to follow instructions found in notes, and no tool can destroy data.
- **Logs** record tool and method names, never note contents. HTTP access logs drop query strings, so OAuth codes, state and transaction IDs never reach the hosting provider's logs.
- **Activity stats:** see below.
- **Container:** the server runs as an unprivileged user (uid 10001) with `no_new_privs`; the app code is read-only to it. The entrypoint runs as root only to hand the OAuth storage directory to that user.

## Activity stats
When a Postgres database is configured (`SB_DATABASE_URL`, or `DATABASE_URL` from a Railway Postgres service) and `SB_STATS` isn't `off`, every successful tool call writes one row to `activity_events`:
- time, tool name, action (`read`, `write`, `search` or `interaction`), a weight, and the client label (`claude.ai`, `claude-code`, `local` or `other`, from the token's client ID; no emails or tokens);
- the vault paths of the notes the call touched (for `search`, the top 5 hits), and the paths it created;
- the `tags`, `projects`, `people` and `orgs` in those notes' frontmatter at that moment.

What is **not** stored: note contents, tool arguments, and, by default, search text. `SB_STATS_LOG_QUERIES=true` stores search queries (truncated to 200 characters); leave it off unless you want them, since queries can reveal more than paths. Failed calls and routine tools (`context`, `health`, `follow_ups_due`, `activity`) aren't recorded.

Events older than `SB_STATS_RETENTION_DAYS` (default 730) are deleted at startup and daily. The `activity` tool only returns aggregates, never raw events, and sits behind the same login and allowlist as every other tool. On Railway, connect over the private network (`postgres.railway.internal`, via `${{Postgres.DATABASE_URL}}`) and don't give the database a public TCP proxy. The database URL is a secret: it's never logged. Recording runs on a background thread, so a database outage drops events (with a warning in the logs) but never fails or slows a tool call. To erase the history, `TRUNCATE activity_events` or delete the database; `SB_STATS=off` stops recording and hides the tool.

## Out of scope
- Anything a connected AI client does with notes it was allowed to read.
- Compromise of the Google, AWS, Railway or GitHub accounts behind a deployment. Protect them with hardware-key 2FA.
