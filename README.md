# second-brain-mcp

**One shared, people-first second brain for every Claude you use.**

An open-source [MCP](https://modelcontextprotocol.io) server that turns an Obsidian vault into memory shared across Claude.ai (web, desktop, mobile), Claude Code on any computer, and Claude Desktop. Notes stay plain Markdown, so you can still read and edit them in Obsidian.

```mermaid
flowchart LR
  A["Claude.ai: web, desktop, phone"] -->|HTTPS + OAuth| S
  B["Claude Code, any computer"] -->|HTTPS + OAuth| S
  S["second-brain-mcp<br/>Railway"] --> F[("S3: your vault<br/>private, KMS, versioned")]
  O["Obsidian"] <-->|Remotely Save| F
```

## Why
- **People first.** People, organizations, meetings, and how people connect to each other are built in. Connections are written on both people's notes. Every conversation is logged on each attendee, with `last_contact` and follow-ups tracked.
- **One brain, everywhere.** Every Claude session reads from and writes to the same vault, so context carries across chats, devices and `/compact`.
- **Your data stays yours.** Plain `.md` files in your own S3 bucket. No hard deletes; every version is kept.
- **Secure by default.** HTTP mode won't start without OAuth, and only allowlisted, verified Google accounts get in. See [SECURITY.md](SECURITY.md).

## Tools

| Notes | | People | |
|---|---|---|---|
| `context` | session start: rules, Home, relevant notes, follow-ups | `person` | full profile, connections, log, meetings, mentions |
| `search` | keywords over names, frontmatter and content | `upsert_person` | create/update; matches aliases to avoid duplicates |
| `read` | one or more notes | `log_interaction` | Meeting note + each attendee's Log + Daily |
| `capture` | quick note to the Inbox | `connect` | relationship, written both ways (`reports_to` ↔ `manages`, …) |
| `write` / `patch` | create / targeted edit | `who_knows` | people at an org; connection path to someone |
| `link` | link two notes both ways | `follow_ups_due` | overdue and upcoming follow-ups |
| `daily` | append to today's note | `people_graph` | nodes + edges, filter by org/context |
| `archive` | the only "delete": moves to `Archive/` | | |
| `health` | broken links, orphans, inbox, stale projects | | |

## Vault format
PARA folders plus `People/`, `Organizations/`, `Meetings/`, `Daily/` and `00-Inbox/`. YAML frontmatter on every note, and `[[wikilinks]]` by bare name. See [`examples/vault`](examples/vault) (all fictional): copy it to start your own vault, and edit its `CLAUDE.md` to change the rules Claude follows.

## Quick start (local)
```bash
git clone https://github.com/liorsolomon/second-brain-mcp && cd second-brain-mcp
uv sync
claude mcp add second-brain -e SB_VAULT_PATH="$PWD/examples/vault" -- uv --directory "$PWD" run second-brain-mcp
```

## Deploy to the cloud
1. [AWS setup](docs/aws-setup.md): Terraform for the bucket, KMS key, least-privilege IAM user and CloudTrail
2. [Deploy to Railway and connect Claude](docs/deploy-and-connect.md)

## Roadmap
- [ ] Semantic search: heading-level chunks, pgvector + full-text, merged by rank, with frontmatter filters
- [ ] `/brain` Claude Code skill for the session routine
- [ ] Persistent OAuth client storage (survives restarts)
- [ ] Optional field-level encryption for contact details

## Development
```bash
uv sync
uvx pre-commit install
uv run ruff check . && uv run pyright && uv run pytest -q
```

Inspired by [Vasallo94/obsidian-mcp-server](https://github.com/Vasallo94/obsidian-mcp-server), [sweir1/obsidian-brain](https://github.com/sweir1/obsidian-brain), [neverprepared/mcp-obsidian-second-brain](https://github.com/neverprepared/mcp-obsidian-second-brain) and [hess0ul/second-brain](https://github.com/hess0ul/second-brain).

[MIT](LICENSE)
