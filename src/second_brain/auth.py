"""Authentication: Google OAuth via FastMCP's OAuth proxy, plus an email allowlist
enforced on every MCP request (not just at login)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .config import Settings

if TYPE_CHECKING:
    from key_value.aio.protocols import AsyncKeyValue

log = logging.getLogger("second_brain.auth")

STORAGE_KEY_SALT = "second-brain-oauth-storage"


def build_client_storage(settings: Settings) -> AsyncKeyValue | None:
    """Fernet-encrypted file store for the OAuth proxy's state, under SB_OAUTH_STORAGE_DIR.

    Returns None when no directory is configured, so FastMCP falls back to its own
    (ephemeral, in-container) store. The subdirectory is named after a fingerprint of the
    encryption key, so rotating the key starts from a clean store instead of failing to decrypt.
    """
    if not settings.oauth_storage_dir:
        return None
    from cryptography.fernet import Fernet
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key
    from key_value.aio.stores.filetree import (
        FileTreeStore,
        FileTreeV1CollectionSanitizationStrategy,
        FileTreeV1KeySanitizationStrategy,
    )
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    secret = settings.oauth_storage_key or settings.jwt_signing_key
    assert secret, "SB_OAUTH_STORAGE_KEY or SB_JWT_SIGNING_KEY is required for SB_OAUTH_STORAGE_DIR"
    key = derive_jwt_key(high_entropy_material=secret.get_secret_value(), salt=STORAGE_KEY_SALT)
    directory = Path(settings.oauth_storage_dir).expanduser() / hashlib.sha256(key).hexdigest()[:12]
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise RuntimeError(f"SB_OAUTH_STORAGE_DIR is not writable: {settings.oauth_storage_dir}") from e
    store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    log.info("OAuth state persisted under %s", settings.oauth_storage_dir)
    # A value that won't decrypt (e.g. after a key change) is a cache miss: the client re-registers.
    return FernetEncryptionWrapper(key_value=store, fernet=Fernet(key=key), raise_on_decryption_error=False)


def build_auth_provider(settings: Settings):
    if settings.auth == "none":
        return None
    from fastmcp.server.auth.providers.google import GoogleProvider

    assert (
        settings.base_url and settings.google_client_id and settings.google_client_secret and settings.jwt_signing_key
    )
    provider = GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        base_url=settings.base_url,
        required_scopes=["openid", "email"],
        jwt_signing_key=settings.jwt_signing_key.get_secret_value(),
        client_storage=build_client_storage(settings),
        # Without this, every MCP request makes two calls to Google before it runs.
        cache_ttl_seconds=settings.token_cache_ttl_seconds or None,
        # Claude.ai and Claude Code callback URLs; loopback ports for local clients.
        # ChatGPT callback patterns can be opted in with SB_EXTRA_REDIRECT_URIS.
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:*",
            "http://127.0.0.1:*",
            *settings.extra_redirect_uris,
        ],
    )
    # The OAuth proxy always enables open registration; client ID metadata documents
    # (CIMD) stay on either way. Disabling it removes POST /register and its
    # advertisement in the authorization-server metadata.
    if provider.client_registration_options is not None:
        provider.client_registration_options.enabled = settings.allow_dynamic_client_registration
    return provider


def is_allowed(claims: dict[str, Any] | None, allowed: list[str]) -> bool:
    if not claims or not allowed:
        return False
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified")
    return bool(email) and verified in (True, "true", "True") and email in allowed


class AllowlistMiddleware(Middleware):
    """Rejects any request whose verified Google email is not on the allowlist."""

    def __init__(self, allowed_emails: list[str]):
        self.allowed = [e.lower() for e in allowed_emails]

    async def on_request(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        if token is None or not is_allowed(getattr(token, "claims", None), self.allowed):
            log.warning("denied request method=%s", context.method)
            raise AuthorizationError("Not authorized for this second brain")
        return await call_next(context)
