"""Authentication: Google OAuth via FastMCP's OAuth proxy, plus an email allowlist
enforced on every MCP request (not just at login)."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .config import Settings

log = logging.getLogger("second_brain.auth")


def build_auth_provider(settings: Settings):
    if settings.auth == "none":
        return None
    from fastmcp.server.auth.providers.google import GoogleProvider

    assert (
        settings.base_url and settings.google_client_id and settings.google_client_secret and settings.jwt_signing_key
    )
    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        base_url=settings.base_url,
        required_scopes=["openid", "email"],
        jwt_signing_key=settings.jwt_signing_key.get_secret_value(),
        # Claude.ai and Claude Code callback URLs; loopback ports for local clients.
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
    )


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
