"""Concurrency and caching: the vault's versioned note cache, the server lock, and token verification caching."""

import asyncio
import logging
import threading
import time
from collections import Counter

import boto3
import httpx2
import pytest
from conftest import TODAY
from fastmcp import Client
from fastmcp.exceptions import ToolError
from moto import mock_aws
from pydantic import SecretStr

from second_brain.auth import build_auth_provider
from second_brain.config import Settings
from second_brain.server import build_server
from second_brain.storage import LocalStorage, S3Storage
from second_brain.vault import Vault


class CountingStorage:
    """Delegates to a real Storage, counting calls and optionally sleeping in `read`."""

    def __init__(self, inner, read_delay: float = 0.0):
        self.inner, self.read_delay = inner, read_delay
        self.calls: Counter[str] = Counter()
        self.active_reads = self.max_active_reads = 0
        self._lock = threading.Lock()

    def versions(self):
        self.calls["versions"] += 1
        return self.inner.versions()

    def list(self):
        self.calls["list"] += 1
        return self.inner.list()

    def read(self, path):
        with self._lock:
            self.calls["read"] += 1
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            if self.read_delay:
                time.sleep(self.read_delay)
            return self.inner.read(path)
        finally:
            with self._lock:
                self.active_reads -= 1

    def write(self, path, content):
        self.calls["write"] += 1
        return self.inner.write(path, content)

    def move(self, src, dst):
        self.calls["move"] += 1
        return self.inner.move(src, dst)

    def exists(self, path):
        self.calls["exists"] += 1
        return self.inner.exists(path)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def counted(vault_dir):
    return CountingStorage(LocalStorage(vault_dir))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cached_vault(counted, clock):
    return Vault(counted, today=lambda: TODAY, listing_ttl=5.0, clock=clock)


def edit_externally(vault_dir, rel, text):
    """Simulate another writer (e.g. Obsidian on another device) changing a file."""
    p = vault_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- storage versions


def test_local_versions_match_write_and_change_with_content(tmp_path):
    s = LocalStorage(tmp_path)
    v1 = s.write("a.md", "one")
    assert s.versions() == {"a.md": v1}
    v2 = s.write("a.md", "two!")
    assert v2 != v1 and s.versions() == {"a.md": v2}


@mock_aws
def test_s3_versions_are_etags_and_match_write():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="brain")
    s = S3Storage("brain", prefix="vault", client=client)
    v1 = s.write("People/Dana.md", "hello")
    client.put_object(Bucket="brain", Key="vault/.obsidian/x.md", Body=b"hidden")
    assert s.versions() == {"People/Dana.md": v1}
    v2 = s.write("People/Dana.md", "hello again")
    assert v2 != v1 and s.versions() == {"People/Dana.md": v2}


# --------------------------------------------------------------------------- vault cache


def test_notes_are_read_once_across_calls(cached_vault, counted, clock):
    first = len(cached_vault.notes())
    reads = counted.calls["read"]
    assert reads >= first
    for _ in range(5):
        cached_vault.resolve("Dana Rivers")
        cached_vault.search("robot")
        clock.now += 10  # past the TTL: storage is listed again, but unchanged notes are not re-read
    assert counted.calls["read"] == reads
    assert counted.calls["versions"] >= 5


def test_listing_is_reused_within_ttl(cached_vault, counted, clock):
    cached_vault.notes()
    for _ in range(10):
        cached_vault.resolve("Dana Rivers")
    assert counted.calls["versions"] == 1
    clock.now += 5
    cached_vault.resolve("Dana Rivers")
    assert counted.calls["versions"] == 2


def test_external_change_shows_after_ttl_and_only_changed_note_is_reread(cached_vault, counted, clock, vault_dir):
    assert "Robots" not in cached_vault.require("Priya Nair").body
    reads = counted.calls["read"]
    edit_externally(
        vault_dir, "People/Priya Nair.md", "---\ntype: person\n---\n# Priya Nair\n\nRobots, edited elsewhere\n"
    )
    assert "Robots" not in cached_vault.require("Priya Nair").body  # within the TTL
    clock.now += 5
    assert "edited elsewhere" in cached_vault.require("Priya Nair").body
    assert counted.calls["read"] == reads + 1


def test_external_delete_drops_note_after_ttl(cached_vault, clock, vault_dir):
    assert cached_vault.resolve("Sam Okafor") is not None
    (vault_dir / "People" / "Sam Okafor.md").rename(vault_dir / "Sam.bak")
    clock.now += 5
    assert cached_vault.resolve("Sam Okafor") is None


def test_own_writes_are_visible_immediately(cached_vault, counted):
    cached_vault.notes()
    cached_vault.create("Resources", "Fresh Note", {"type": "note"}, "# Fresh Note\n")
    assert cached_vault.resolve("Fresh Note") is not None
    assert counted.calls["versions"] == 1  # found via the updated listing, not a new LIST


def test_archive_updates_listing(cached_vault):
    cached_vault.notes()
    cached_vault.archive("Sam Okafor")
    paths = [n.path for n in cached_vault.notes()]
    assert "Archive/People/Sam Okafor.md" in paths and "People/Sam Okafor.md" not in paths


def test_path_lookup_finds_note_created_elsewhere_within_ttl(cached_vault, vault_dir):
    """daily() resolves by path; a stale listing must not make it overwrite another writer's note."""
    cached_vault.notes()
    edit_externally(
        vault_dir, f"Daily/{TODAY.isoformat()}.md", "---\ntype: daily\n---\n# Today\n\n## Log\n- from phone\n"
    )
    assert "from phone" in cached_vault.daily().body


def test_ttl_zero_lists_every_time(counted, clock):
    v = Vault(counted, today=lambda: TODAY, listing_ttl=0, clock=clock)
    v.resolve("Dana Rivers")
    v.resolve("Dana Rivers")
    assert counted.calls["versions"] == 2


def test_cold_cache_reads_in_parallel(vault_dir):
    slow = CountingStorage(LocalStorage(vault_dir), read_delay=0.05)
    Vault(slow).notes()
    assert slow.max_active_reads > 1


def test_refresh_forgets_everything(cached_vault, counted):
    cached_vault.notes()
    reads = counted.calls["read"]
    cached_vault.refresh()
    cached_vault.notes()
    assert counted.calls["read"] == 2 * reads


# --------------------------------------------------------------------------- server


def make_server(storage, clock=None, ttl=5.0):
    vault = Vault(storage, today=lambda: TODAY, listing_ttl=ttl, clock=clock or time.monotonic)
    return build_server(Settings(_env_file=None, transport="stdio", auth="none", stats=False), vault=vault)


async def test_failed_call_does_not_leave_unsaved_edits_in_cache(counted):
    server = make_server(counted)
    async with Client(server) as c:
        # The body edit happens before set_meta is rejected; it must not leak into later reads.
        with pytest.raises(ToolError, match="cannot be changed"):
            await c.call_tool("patch", {"name": "Dana Rivers", "append": "UNSAVED EDIT", "set_meta": {"type": "note"}})
        r = await c.call_tool("read", {"names": ["Dana Rivers"]})
    assert "UNSAVED EDIT" not in r.structured_content["result"][0]["body"]


async def test_write_tools_list_storage_first(counted, clock, vault_dir):
    """A patch must apply on top of another writer's change made within the read TTL, not overwrite it."""
    server = make_server(counted, clock)
    async with Client(server) as c:
        await c.call_tool("read", {"names": ["Priya Nair"]})
        edit_externally(
            vault_dir, "People/Priya Nair.md", "---\ntype: person\n---\n# Priya Nair\n\nAdded on the phone\n"
        )
        await c.call_tool("patch", {"name": "Priya Nair", "append": "Added by Claude"})
    text = (vault_dir / "People" / "Priya Nair.md").read_text()
    assert "Added on the phone" in text and "Added by Claude" in text


async def test_tools_do_not_block_the_event_loop(vault_dir):
    """Vault work runs in worker threads: the loop keeps serving while a slow storage call is in flight."""
    server = make_server(CountingStorage(LocalStorage(vault_dir), read_delay=0.05))
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    async with Client(server) as c:
        t = asyncio.create_task(ticker())
        start = time.perf_counter()
        await c.call_tool("read", {"names": ["Dana Rivers"]})  # cold cache: every note is read
        elapsed = time.perf_counter() - start
        done.set()
        await t
    assert elapsed > 0.05
    assert ticks >= int(elapsed / 0.01) // 2


async def test_concurrent_clients_lose_no_updates(vault_dir):
    storage = CountingStorage(LocalStorage(vault_dir))
    server = make_server(storage)

    async def session(cid: int):
        async with Client(server) as c:
            for r in range(3):
                await c.call_tool("write", {"folder": "Resources/Load", "name": f"Client {cid} note {r}", "body": "x"})
                await c.call_tool("upsert_person", {"name": f"Load Person {cid}", "notes": f"fact {r}"})
                await c.call_tool("patch", {"name": "Acme Robotics", "append": f"- client {cid} round {r}"})
                await c.call_tool("read", {"names": ["Acme Robotics", f"Load Person {cid}"]})

    await asyncio.wait_for(asyncio.gather(*(session(i) for i in range(5))), timeout=30)

    org = (vault_dir / "Organizations" / "Acme Robotics.md").read_text()
    assert all(f"- client {cid} round {r}" in org for cid in range(5) for r in range(3))
    for cid in range(5):
        person = (vault_dir / "People" / f"Load Person {cid}.md").read_text()
        assert all(f"fact {r}" in person for r in range(3))
    assert len(list((vault_dir / "Resources" / "Load").glob("*.md"))) == 15
    # Each note is parsed once; later calls only re-read what this process didn't write itself.
    assert storage.calls["read"] < 2 * len(storage.inner.list())


# --------------------------------------------------------------------------- token verification cache


def auth_settings(**kw):
    return Settings(
        _env_file=None,
        base_url="https://brain.example.com",
        google_client_id="client-id",
        google_client_secret=SecretStr("client-secret"),
        jwt_signing_key=SecretStr("signing-key"),
        **kw,
    )


def mock_google(monkeypatch):
    calls: Counter[str] = Counter()

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls[request.url.path] += 1
        if request.url.path == "/tokeninfo":
            return httpx2.Response(
                200,
                json={
                    "aud": "client-id",
                    "sub": "123",
                    "scope": "openid https://www.googleapis.com/auth/userinfo.email",
                    "expires_in": "3599",
                    "email": "owner@example.com",
                    "email_verified": "true",
                },
            )
        return httpx2.Response(200, json={"email": "owner@example.com", "verified_email": True})

    real = httpx2.AsyncClient
    monkeypatch.setattr(
        "fastmcp.server.auth.providers.google.httpx2.AsyncClient",
        lambda **kw: real(transport=httpx2.MockTransport(handler), **kw),
    )
    return calls


async def test_google_token_verification_is_cached(monkeypatch, caplog):
    calls = mock_google(monkeypatch)
    verifier = build_auth_provider(auth_settings())._token_validator  # type: ignore[union-attr]
    secret = "ya29.super-secret-google-token"
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            tok = await verifier.verify_token(secret)
            assert tok is not None and tok.claims["email"] == "owner@example.com"
    assert calls == {"/tokeninfo": 1, "/oauth2/v2/userinfo": 1}
    assert secret not in caplog.text
    assert secret not in repr(verifier._cache._entries.keys())  # keyed by a hash, not the token


async def test_google_token_cache_can_be_disabled(monkeypatch):
    calls = mock_google(monkeypatch)
    verifier = build_auth_provider(auth_settings(token_cache_ttl_seconds=0))._token_validator  # type: ignore[union-attr]
    for _ in range(3):
        assert await verifier.verify_token("ya29.token") is not None
    assert calls["/tokeninfo"] == 3


def test_token_cache_never_outlives_the_token(monkeypatch):
    verifier = build_auth_provider(auth_settings())._token_validator  # type: ignore[union-attr]
    from fastmcp.server.auth.auth import AccessToken

    verifier._cache.set("t", AccessToken(token="t", client_id="c", scopes=[], expires_at=int(time.time()) - 1))
    assert verifier._cache.get("t") == (False, None)
