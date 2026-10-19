"""Runtime configuration. Everything deployment-specific comes from the environment."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SB_", env_file=".env", extra="ignore")

    # Storage
    storage: Literal["local", "s3"] = "local"
    vault_path: str = "./examples/vault"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    s3_region: str | None = None
    s3_kms_key_id: str | None = None
    # How long a vault listing is reused by read tools before storage is listed again. This
    # process's own writes show at once, and write tools always list first; only notes changed by
    # other writers (e.g. Obsidian on another device) can take this long to appear. 0 = every call.
    listing_ttl_seconds: float = Field(default=5.0, ge=0)

    # Transport
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"  # noqa: S104 - container must listen on all interfaces
    port: int = 8000

    # Auth (required for http transport)
    auth: Literal["google", "none"] = "google"
    base_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    jwt_signing_key: SecretStr | None = None
    # Persistent OAuth state (client registrations, tokens). Point this at a mounted volume so
    # logins survive redeploys. Unset = FastMCP's default, a directory inside the container.
    oauth_storage_dir: str | None = None
    # Encrypts the OAuth state at rest. Unset = derived from SB_JWT_SIGNING_KEY.
    oauth_storage_key: SecretStr | None = None
    # NoDecode: read the env var as a comma-separated string, not JSON.
    allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Additional OAuth redirect URI patterns for clients such as ChatGPT. Comma-separated;
    # keep the default Claude and local callbacks in auth.py.
    extra_redirect_uris: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Open dynamic client registration (POST /register). Off by default: Claude.ai and
    # Claude Code identify themselves with client ID metadata documents instead, and open
    # registration lets anyone write unlimited, never-expiring records to OAuth storage.
    # Turn on only for an MCP client that can't use metadata documents.
    allow_dynamic_client_registration: bool = False
    # Reuse a successful Google token verification (tokeninfo + userinfo) for up to this many
    # seconds, never past the token's own expiry. Keyed by a SHA-256 of the token, in memory only.
    # Removing an email from SB_ALLOWED_EMAILS still takes effect at once; a token revoked at
    # Google stays accepted until the cached entry expires. 0 = verify with Google on every request.
    token_cache_ttl_seconds: int = Field(default=3600, ge=0)

    # Activity statistics: one small event per tool call, kept in Postgres (see SECURITY.md).
    # On by default; SB_STATS=off disables recording and the `activity` tool. Without a
    # database URL the server logs a warning and runs without it.
    stats: bool = True
    # SB_DATABASE_URL, or DATABASE_URL as provided by a Railway Postgres service.
    database_url: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("SB_DATABASE_URL", "DATABASE_URL", "database_url")
    )
    stats_retention_days: int = Field(default=730, ge=1)
    # Store the text of `search` queries. Off by default: queries can be more revealing than note paths.
    stats_log_queries: bool = False

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, v: object) -> object:
        if isinstance(v, str):
            return [e.strip().lower() for e in v.split(",") if e.strip()]
        if isinstance(v, list):
            return [str(e).strip().lower() for e in v]
        return v

    @field_validator("extra_redirect_uris", mode="before")
    @classmethod
    def _split_redirect_uris(cls, v: object) -> object:
        if isinstance(v, str):
            return [uri.strip() for uri in v.split(",") if uri.strip()]
        if isinstance(v, list):
            return [str(uri).strip() for uri in v if str(uri).strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.storage == "s3" and not self.s3_bucket:
            raise ValueError("SB_S3_BUCKET is required when SB_STORAGE=s3")
        if self.transport == "http":
            if self.auth == "none":
                raise ValueError("SB_AUTH=none is not allowed with the http transport")
            missing = [
                name
                for name, val in {
                    "SB_BASE_URL": self.base_url,
                    "SB_GOOGLE_CLIENT_ID": self.google_client_id,
                    "SB_GOOGLE_CLIENT_SECRET": self.google_client_secret,
                    "SB_JWT_SIGNING_KEY": self.jwt_signing_key,
                }.items()
                if not val
            ]
            if not self.allowed_emails:
                missing.append("SB_ALLOWED_EMAILS")
            if missing:
                raise ValueError(f"Missing required settings for http transport: {', '.join(missing)}")
        return self
