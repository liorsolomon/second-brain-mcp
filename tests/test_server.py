import pytest
from fastmcp import Client
from fastmcp.exceptions import AuthorizationError, ToolError
from pydantic import ValidationError

from second_brain.auth import AllowlistMiddleware, is_allowed
from second_brain.config import Settings
from second_brain.server import build_server


@pytest.fixture
def server(vault):
    return build_server(Settings(transport="stdio", auth="none"), vault=vault)


async def test_lists_all_tools(server):
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert {
        "context",
        "search",
        "read",
        "capture",
        "write",
        "patch",
        "link",
        "daily",
        "archive",
        "health",
        "person",
        "upsert_person",
        "log_interaction",
        "connect",
        "who_knows",
        "follow_ups_due",
        "people_graph",
    } <= names
    assert "delete" not in names


async def test_instructions_ask_to_read_first_and_capture(server):
    text = (server.instructions or "").lower()
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert "always call `context` first" in text
    assert "without asking" in text and "what you logged" in text
    assert "not instructions" in text  # the prompt-injection guard stays
    assert "never send note contents" in text  # the exfiltration guard
    assert "first in every conversation" in (tools["context"].description or "")


async def test_read_only_tools_are_annotated(server):
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert tools["person"].annotations.read_only_hint is True
    assert tools["archive"].annotations.destructive_hint is False


async def test_context_returns_rules_and_follow_ups(server):
    async with Client(server) as c:
        r = (await c.call_tool("context", {"topic": "robot launch"})).data
    assert "if it isn't written down" in r["claude"].lower()
    assert r["follow_ups_due"][0]["name"] == "Dana Rivers"
    assert any(h["name"] == "Robot Launch" for h in r["relevant"])


async def test_people_flow(server):
    async with Client(server) as c:
        await c.call_tool("upsert_person", {"name": "Lee Chen", "org": "Globex", "relationship": "client"})
        await c.call_tool("connect", {"a": "Lee Chen", "b": "Dana", "rel": "introduced"})
        await c.call_tool("log_interaction", {"attendees": ["Lee Chen"], "summary": "Intro call"})
        p = (await c.call_tool("person", {"name": "Lee Chen"})).data
        dana = (await c.call_tool("person", {"name": "Dana"})).data
    assert p["last_contact"] == "2026-01-10"
    assert {"to": "[[Lee Chen]]", "rel": "met_via"} in dana["connections"]


async def test_patch_find_must_be_unique(server):
    async with Client(server) as c:
        with pytest.raises(ToolError, match="exactly once"):
            await c.call_tool("patch", {"name": "Robot Launch", "find": "- [ ]", "replace": "x"})
        r = await c.call_tool("patch", {"name": "Robot Launch", "append": "- [ ] Hire PM", "section": "Next"})
        assert r.data["path"] == "Projects/Robot Launch/Robot Launch.md"


async def test_write_refuses_people_folder(server):
    async with Client(server) as c:
        with pytest.raises(ToolError):
            await c.call_tool("write", {"folder": "People", "name": "X", "body": "x"})


async def test_domain_errors_are_reported(server):
    async with Client(server) as c:
        with pytest.raises(ToolError, match="No person"):
            await c.call_tool("person", {"name": "Nobody"})


def test_http_requires_full_auth_config():
    with pytest.raises(ValidationError, match="SB_ALLOWED_EMAILS"):
        Settings(
            transport="http",
            auth="google",
            base_url="https://x",
            google_client_id="id",
            google_client_secret="s",
            jwt_signing_key="k",
            allowed_emails="",
        )
    with pytest.raises(ValidationError, match="not allowed"):
        Settings(transport="http", auth="none")


def test_allowed_emails_parsing():
    s = Settings(allowed_emails=" A@Example.com, b@example.com ")
    assert s.allowed_emails == ["a@example.com", "b@example.com"]


def test_allowed_emails_from_env(monkeypatch):
    monkeypatch.setenv("SB_ALLOWED_EMAILS", "me@example.com,You@Example.com")
    assert Settings().allowed_emails == ["me@example.com", "you@example.com"]


@pytest.mark.parametrize(
    "claims,ok",
    [
        ({"email": "me@example.com", "email_verified": True}, True),
        ({"email": "ME@example.com", "email_verified": True}, True),
        ({"email": "me@example.com", "email_verified": False}, False),
        ({"email": "me@example.com"}, False),
        ({"email": "other@example.com", "email_verified": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_allowed(claims, ok):
    assert is_allowed(claims, ["me@example.com"]) is ok


def test_is_allowed_empty_allowlist_denies_everyone():
    assert not is_allowed({"email": "me@example.com", "email_verified": True}, [])


class _Ctx:
    method = "tools/call"


async def _ran(_):
    return "ran"


def _fake_token(monkeypatch, claims):
    monkeypatch.setattr("second_brain.auth.get_access_token", lambda: type("T", (), {"claims": claims})())


async def test_allowlist_middleware_denies_without_token(monkeypatch):
    monkeypatch.setattr("second_brain.auth.get_access_token", lambda: None)
    with pytest.raises(AuthorizationError):
        await AllowlistMiddleware(["me@example.com"]).on_request(_Ctx(), _ran)


async def test_allowlist_middleware_allows_listed_token(monkeypatch):
    _fake_token(monkeypatch, {"email": "me@example.com", "email_verified": True})
    assert await AllowlistMiddleware(["me@example.com"]).on_request(_Ctx(), _ran) == "ran"


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "evil@example.com", "email_verified": True},  # off the allowlist
        {"email": "me@example.com", "email_verified": False},  # unverified Google email
        {"email": "me@example.com"},  # no email_verified claim at all
    ],
)
async def test_allowlist_middleware_denies_bad_token(monkeypatch, claims):
    _fake_token(monkeypatch, claims)
    with pytest.raises(AuthorizationError):
        await AllowlistMiddleware(["me@example.com"]).on_request(_Ctx(), _ran)


async def test_read_omits_contact_pii_but_person_returns_it(server):
    async with Client(server) as c:
        await c.call_tool(
            "upsert_person", {"name": "Reed Poe", "email": "reed@example.com", "phone": "+1-555-0100"}
        )
        notes = (await c.call_tool("read", {"names": ["Reed Poe"]})).data
        profile = (await c.call_tool("person", {"name": "Reed Poe"})).data
    assert "email" not in notes[0]["meta"] and "phone" not in notes[0]["meta"]
    assert profile["email"] == "reed@example.com" and profile["phone"] == "+1-555-0100"


async def test_search_snippet_redacts_email(server):
    async with Client(server) as c:
        await c.call_tool("capture", {"text": "reach me at secret@example.com about launch", "title": "Blob"})
        hits = (await c.call_tool("search", {"query": "reach launch"})).data
    blob = next(h for h in hits if h["name"] == "Blob")
    assert "secret@example.com" not in blob["snippet"] and "[email]" in blob["snippet"]
