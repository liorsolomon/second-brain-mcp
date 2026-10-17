# Contributing

Thanks for helping. A few ground rules:

1. **Never commit real personal data.** Tests, examples and docs use fictional people only, like those in `examples/vault`. Don't paste notes from your own vault.
2. **Install the pre-commit hooks** (secret scanning + lint): `uvx pre-commit install`
3. **Run the checks** before opening a PR:
   ```bash
   uv sync
   uv run ruff check . && uv run pyright && uv run pytest -q
   ```
4. **No destructive tools.** New tools must not hard-delete data. Use `archive`.
5. Keep the vault format plain Markdown + YAML frontmatter, so it stays readable in Obsidian with no plugins.
