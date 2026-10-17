import secrets
from pathlib import Path

import pytest

from second_brain import server as server_module
from second_brain.auth import build_auth_provider, build_client_storage
from second_brain.config import Settings

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


def test_no_storage_dir_keeps_fastmcp_default():
    assert build_client_storage(http_settings()) is None


async def test_state_survives_a_new_process(tmp_path: Path):
    settings = http_settings(oauth_storage_dir=str(tmp_path))
    first = build_client_storage(settings)
    assert first is not None
    await first.put("client-1", {"client_id": "client-1", "redirect_uris": ["http://localhost:1234"]}, collection="clients")

    # A redeploy builds a fresh store over the same volume.
    second = build_client_storage(settings)
    assert second is not None
    assert await second.get("client-1", collection="clients") == {
        "client_id": "client-1",
        "redirect_uris": ["http://localhost:1234"],
    }


async def test_state_is_encrypted_at_rest(tmp_path: Path):
    store = build_client_storage(http_settings(oauth_storage_dir=str(tmp_path)))
    assert store is not None
    await store.put("client-1", {"refresh_token": "plaintext-refresh-token"}, collection="tokens")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files
    assert all(b"plaintext-refresh-token" not in p.read_bytes() for p in files)


async def test_key_change_is_a_cache_miss_not_a_crash(tmp_path: Path):
    old = build_client_storage(http_settings(oauth_storage_dir=str(tmp_path)))
    assert old is not None
    await old.put("client-1", {"client_id": "client-1"}, collection="clients")

    rotated = build_client_storage(http_settings(oauth_storage_dir=str(tmp_path), oauth_storage_key=secrets.token_urlsafe(32)))
    assert rotated is not None
    assert await rotated.get("client-1", collection="clients") is None


def test_separate_storage_key_takes_precedence(tmp_path: Path):
    build_client_storage(http_settings(oauth_storage_dir=str(tmp_path)))
    build_client_storage(http_settings(oauth_storage_dir=str(tmp_path), oauth_storage_key=secrets.token_urlsafe(32)))
    # One fingerprinted subdirectory per key.
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 2


def test_unwritable_storage_dir_fails_fast(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            build_client_storage(http_settings(oauth_storage_dir=str(locked / "oauth")))
    finally:
        locked.chmod(0o700)


def test_google_provider_uses_persistent_storage(tmp_path: Path):
    provider = build_auth_provider(http_settings(oauth_storage_dir=str(tmp_path)))
    assert provider is not None
    assert any(p.is_dir() for p in tmp_path.iterdir())


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_main_disables_banner(monkeypatch, transport):
    calls = []

    class FakeServer:
        def run(self, **kwargs):
            calls.append(kwargs)

    settings = http_settings() if transport == "http" else Settings(transport="stdio", auth="none")
    monkeypatch.setattr(server_module, "Settings", lambda: settings)
    monkeypatch.setattr(server_module, "build_server", lambda settings: FakeServer())
    server_module.main()
    assert calls[0]["show_banner"] is False
