# Make every Claude use your second brain

Connecting the server makes the vault *available*. This guide makes Claude actually **use** it in every chat, on every device:

1. **Read first:** start each conversation by pulling relevant context from the vault.
2. **Write as you go:** when a new topic, project, idea, decision or person comes up, log it without being asked.

No single setting covers every app, and Claude always decides when to call a tool, so nothing here is a hard guarantee. In practice, the same short rule in a few places makes it reliable.

## Where the rules go

| Layer | Reaches | Setup | What goes there |
|---|---|---|---|
| [Server instructions](#1-server-instructions-built-in) | Every client connected to the server | None (built in) | The short version of both rules |
| [Your vault's `CLAUDE.md`](#2-your-vaults-claudemd) | Every client, once `context` runs | Edit one note | The full rules, in your own words |
| [Claude.ai preferences](#3-claudeai-desktop-mobile-and-cowork) | Claude.ai web, desktop, mobile (and Cowork, which uses the same account) | Once per account | One sentence that makes sure `context` gets called |
| [Claude Code memory and hook](#4-claude-code) | Claude Code on one computer | Once per computer | The same sentence, plus an optional SessionStart reminder |
| [A skill](#5-optional-a-skill-for-the-playbook) (optional) | Wherever you install it | Once | The detailed playbook and on-demand routines |

The vault's `CLAUDE.md` holds the actual rules. The account and computer settings exist only to make sure Claude calls `context` (which returns `CLAUDE.md`) at the start.

### 1. Server instructions (built in)
The server sends instructions to every MCP client at connection time: start with `context`, and if it isn't written down, it's lost. You don't need to do anything for this, but clients treat these instructions as background, so on their own they're a weak signal. The layers below make them stick.

### 2. Your vault's `CLAUDE.md`
`context` returns your vault's `CLAUDE.md` as the rules for the session. It's one note, so editing it changes behavior everywhere at once. The [example vault's CLAUDE.md](../examples/vault/CLAUDE.md) includes a **Read first, capture as you go** section. Copy it into your own vault and adjust it to your taste. The parts that matter most:

- **Search before creating**, so Claude updates existing notes instead of making duplicates.
- **Say what was logged** in one line at the end of a reply (`Logged: [[Robot fleet]] (new), updated [[Robot Launch]]`). You see every write and can correct mistakes right away.
- **Say what not to log**: small talk, one-off lookups, anything you mark as private. Without this, the vault fills with noise.

### 3. Claude.ai, desktop, mobile and Cowork
**Settings → Profile → "What personal preferences should Claude consider in responses?"** Add:

> At the start of every conversation, before answering, call the `second-brain` connector's `context` tool with the topic of my first message and follow the vault rules it returns. Whenever a new topic, project, idea, decision or person comes up that isn't in my second brain, add it (search first to avoid duplicates) and tell me in one line what you logged.

Preferences belong to your account, so they apply on web, desktop and mobile. Also make sure the connector is switched on in the chat's tools menu: if it's off, Claude can't call it. If you use Projects, you can repeat the sentence in a project's instructions for extra weight.

### 4. Claude Code
Claude Code reads `~/.claude/CLAUDE.md` in every project on the computer. Add the same sentence there. It's a local file, so do this on each computer (or keep it in your dotfiles).

For a stronger nudge, add a **SessionStart hook** to `~/.claude/settings.json`. Whatever the hook prints is added to the session's context at every start. A hook can't call MCP tools itself; it reminds Claude to.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo \"Before anything else, call the second-brain context tool with this session's topic and follow the vault rules it returns. Log new topics, decisions and people to the vault as they come up.\""
          }
        ]
      }
    ]
  }
}
```

If you already have a `settings.json`, merge the `hooks` key into it rather than replacing the file.

### 5. Optional: a skill for the playbook
Skills load only when a conversation seems to match their description. That's unreliable for "do this in every chat", so keep the always-on rules in the layers above. Skills are good for:
- **The detailed playbook:** where each kind of note goes, tagging conventions, when to `capture` and when to `write`. That's more than you want in every prompt.
- **Routines you start on purpose:** a weekly review, a retrospective from `activity`, inbox triage.

Skills you upload to Claude.ai belong to your account (web, desktop, mobile). Claude Code skills live in `~/.claude/skills/` on each computer, or in a plugin. If you add one, shorten the always-on sentence to "use the second-brain skill" and keep the detail in the skill.

## Check that it's working
- Start a new chat on each device with something the vault knows about. Claude's first action should be a `context` call.
- Mention something new ("I'm starting to look into home batteries"). The reply should end with a `Logged:` line.
- If [activity stats](deploy-and-connect.md#activity-stats) are on, ask Claude to run `activity` for the last 30 days. A healthy setup shows regular `write` events next to the reads. If it's almost all reads, Claude isn't capturing: make the wording in your vault's `CLAUDE.md` firmer.
