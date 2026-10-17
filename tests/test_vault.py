import pytest

from second_brain.vault import Note, VaultError, link_target


def test_link_target():
    assert link_target("[[People/Dana Rivers|Dana]]") == "Dana Rivers"
    assert link_target("Dana Rivers.md") == "Dana Rivers"


def test_resolve_by_name_alias_and_path(vault):
    assert vault.resolve("dana rivers").name == "Dana Rivers"
    assert vault.resolve("Dana").name == "Dana Rivers"
    assert vault.resolve("People/Dana Rivers.md").name == "Dana Rivers"
    assert vault.resolve("Nobody") is None


def test_templates_are_not_notes(vault):
    assert all(not n.path.startswith("Templates/") for n in vault.notes())


def test_create_rejects_duplicates_and_sets_dates(vault):
    n = vault.create("Resources/MCP", "OAuth Notes", {"type": "resource", "tags": []}, "# OAuth Notes\n")
    assert n.meta["created"] == "2026-01-10" and n.meta["updated"] == "2026-01-10"
    with pytest.raises(VaultError):
        vault.create("Resources", "oauth notes", {"type": "resource"}, "x")
    with pytest.raises(VaultError):
        vault.create("Resources", "Dana", {"type": "resource"}, "x")  # alias collision


def test_create_sanitizes_names(vault):
    n = vault.create("00-Inbox", "a/b:c", {"type": "note"}, "x")
    assert n.path == "00-Inbox/a-b-c.md"


def test_append_to_section_top_and_bottom(vault):
    n = Note("x.md", {}, "# X\n\n## Log\n<!-- c -->\n- old\n\n## Other\n- o\n")
    vault.append_to_section(n, "Log", "- new", top=True)
    vault.append_to_section(n, "Log", "- last")
    vault.append_to_section(n, "Related", "- [[Y]]")
    lines = n.body.split("\n")
    assert lines.index("- new") < lines.index("- old") < lines.index("- last") < lines.index("## Other")
    assert n.body.rstrip().endswith("## Related\n- [[Y]]")


def test_append_replaces_empty_placeholder(vault):
    n = Note("x.md", {}, "## Who they are\n-\n\n## Log\n")
    vault.append_to_section(n, "Who they are", "- fact")
    assert "## Who they are\n- fact\n" in n.body and "-\n\n" not in n.body


def test_archive_moves_and_keeps_note(vault, vault_dir):
    n = vault.archive("MCP Transports")
    assert n.path == "Archive/Resources/MCP/MCP Transports.md"
    assert n.meta["status"] == "archived"
    assert (vault_dir / n.path).exists()
    assert not (vault_dir / "Resources/MCP/MCP Transports.md").exists()


def test_daily_creates_once(vault):
    a = vault.daily()
    b = vault.daily()
    assert a.path == b.path == "Daily/2026-01-10.md"


def test_search_ranks_name_matches(vault):
    hits = vault.search("dana")
    assert hits[0].name == "Dana Rivers"
    assert vault.search("pricing proposal", type="meeting")[0].name == "2026-01-05 Robot Launch kickoff"
    assert vault.search("x") == []


def test_links_include_frontmatter(vault):
    assert "Acme Robotics" in vault.require("Dana Rivers").links


def test_health(vault):
    vault.create("00-Inbox", "Loose thought", {"type": "note"}, "Mentions [[Ghost Person]]")
    h = vault.health()
    assert "00-Inbox/Loose thought.md" in h["inbox"]
    assert "Loose thought -> Ghost Person" in h["broken_links"]
    assert h["missing_frontmatter"] == []


def test_links_ignore_code():
    n = Note("x.md", {}, "See [[Real]].\n```python\ndf[[\"A\", \"B\"]]\n```\nand `[[Inline]]`")
    assert n.links == {"Real"}
