"""Runtime configuration. Everything deployment-specific comes from the environment."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # NoDecode: read the env var as a comma-separated string, not JSON.
    allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, v: object) -> object:
        if isinstance(v, str):
            return [e.strip().lower() for e in v.split(",") if e.strip()]
        if isinstance(v, list):
            return [str(e).strip().lower() for e in v]
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
