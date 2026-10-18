from pydantic import SecretStr

from second_brain.auth import build_auth_provider
from second_brain.config import Settings


def test_extra_redirect_uris_are_added_to_oauth_allowlist(monkeypatch):
    class FakeGoogleProvider:
        def __init__(self, **kwargs):
            self.allowed_client_redirect_uris = kwargs["allowed_client_redirect_uris"]
            self.client_registration_options = None

    monkeypatch.setattr("fastmcp.server.auth.providers.google.GoogleProvider", FakeGoogleProvider)
    settings = Settings(
        _env_file=None,
        base_url="https://brain.example.com",
        google_client_id="client-id",
        google_client_secret=SecretStr("client-secret"),
        jwt_signing_key=SecretStr("signing-key"),
        extra_redirect_uris=(
            "https://chatgpt.com/connector/oauth/*, "
            "https://chatgpt.com/connector_platform_oauth_redirect"
        ),
    )

    provider = build_auth_provider(settings)

    assert provider.allowed_client_redirect_uris == [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://chatgpt.com/connector/oauth/*",
        "https://chatgpt.com/connector_platform_oauth_redirect",
    ]
