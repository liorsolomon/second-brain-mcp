"""Regression tests for the 2026-10 security review findings."""

import logging
import logging.config
import secrets

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from second_brain import server as server_module
from second_brain.config import Settings
from second_brain.logs import StripQueryFilter, uvicorn_log_config
from second_brain.server import build_server

JWT_KEY = secrets.token_urlsafe(32)


def http_settings(**overrides) -> Settings:
    values = {
        "transport": "http",
        "auth": "google",
        "base_url": "https://brain.example.test",
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "jwt_signing_key": JWT_KEY,
        "allowed_emails": "me@example.com",
        **overrides,
    }
    return Settings(**values)  # pyright: ignore[reportArgumentType]


# ---------- Finding 1: open dynamic client registration ----------

REGISTRATION = {"redirect_uris": ["http://localhost:1234/callback"], "client_name": "probe"}


def test_dynamic_registration_off_by_default(vault):
    app = build_server(http_settings(), vault=vault).http_app()
    with TestClient(app) as c:
        assert c.post("/register", json=REGISTRATION).status_code == 404
        meta = c.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in meta
    # Claude.ai and Claude Code use client ID metadata documents, which stay available.
    assert meta.get("client_id_metadata_document_supported") is True


def test_dynamic_registration_can_be_enabled(vault):
    app = build_server(http_settings(allow_dynamic_client_registration=True), vault=vault).http_app()
    with TestClient(app) as c:
        assert c.post("/register", json=REGISTRATION).status_code == 201
        assert "registration_endpoint" in c.get("/.well-known/oauth-authorization-server").json()


# ---------- Finding 2: write tool folder guard ----------


@pytest.fixture
def local_server(vault):
    return build_server(Settings(transport="stdio", auth="none"), vault=vault)


@pytest.mark.parametrize("folder", ["People", "./People", "/People", "people", "Meetings/2026", "./Archive/x"])
async def test_write_refuses_reserved_folders_after_normalizing(local_server, folder):
    async with Client(local_server) as c:
        with pytest.raises(ToolError, match="upsert_person"):
            await c.call_tool("write", {"folder": folder, "name": "Sneaky", "body": "x"})


@pytest.mark.parametrize("note_type", ["person", "meeting"])
async def test_write_refuses_reserved_types(local_server, note_type):
    async with Client(local_server) as c:
        with pytest.raises(ToolError, match="upsert_person"):
            await c.call_tool("write", {"folder": "Resources", "name": "Sneaky", "body": "x", "type": note_type})


async def test_write_still_allows_normal_folders(local_server):
    async with Client(local_server) as c:
        r = await c.call_tool("write", {"folder": "./Resources/MCP", "name": "OAuth notes", "body": "x"})
    assert r.data["path"] == "Resources/MCP/OAuth notes.md"


# ---------- Finding 3: query strings in access logs ----------


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", 302),
        None,
    )


def test_filter_strips_query_but_keeps_path():
    record = _access_record("/auth/callback?state=abc&code=SECRET")
    assert StripQueryFilter().filter(record) is True
    assert record.getMessage() == '1.2.3.4:5 - "GET /auth/callback HTTP/1.1" 302'


def test_filter_leaves_other_records_alone():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "plain %s", ("message?with=query",), None)
    StripQueryFilter().filter(record)
    assert record.getMessage() == "plain message?with=query"


def test_uvicorn_log_config_applies_filter(capsys):
    logging.config.dictConfig(uvicorn_log_config())
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/consent?txn_id=SECRET", "1.1", 200
    )
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "/consent" in logged and "SECRET" not in logged and "txn_id" not in logged


def test_main_passes_log_config(monkeypatch):
    calls = []

    class FakeServer:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(server_module, "Settings", lambda: http_settings())
    monkeypatch.setattr(server_module, "build_server", lambda settings: FakeServer())
    server_module.main()
    filters = calls[0]["uvicorn_config"]["log_config"]["handlers"]["access"]["filters"]
    assert "strip_query" in filters
