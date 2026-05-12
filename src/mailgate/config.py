"""Configuration loading: environment variables and (optional) clients.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


CaptchaProvider = Literal["turnstile", "hcaptcha", "recaptcha"]


def _split_csv(value: str) -> list[str]:
    """Split a comma- (or whitespace-) separated string into a clean list."""
    if not value:
        return []
    parts = [p.strip() for p in value.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _split_csv_or_none(value: str) -> Optional[list[str]]:
    parts = _split_csv(value)
    return parts if parts else None


class ClientConfig(BaseModel):
    """One API client (one consumer of the relay)."""

    name: str
    api_key: str = Field(min_length=16)

    # --- Lockdown / access control ---
    allowed_origins: list[str] = Field(default_factory=list)
    allowed_from_addresses: Optional[list[str]] = None
    allowed_to_addresses: Optional[list[str]] = None
    ip_blocklist: list[str] = Field(default_factory=list)
    """IPs / CIDRs that are hard-rejected regardless of other rules."""
    ip_allowlist: list[str] = Field(default_factory=list)
    """When non-empty, ONLY these IPs / CIDRs may use this api_key.
    Empty list = no IP restriction (default). Use this for server-to-server
    callers where the api_key would otherwise be a pure secret.
    Evaluated after the blocklist."""

    # --- Defaults applied when caller omits these fields ---
    default_from: Optional[str] = None
    """Used as 'from' if request omits it. Must still pass allowed_from_addresses."""
    default_to: Optional[list[str]] = None
    """Used as 'to' if request omits it. Caller can override (still scope-checked)."""
    subject_prefix: Optional[str] = None
    """Prepended to every email subject. A separating space is auto-inserted if the
    prefix doesn't already end with whitespace, so '[Antikas]' and '[Antikas] ' both
    yield '[Antikas] Subject'."""

    # --- Rate limiting ---
    rate_limit_per_minute: int = 10
    rate_limit_per_minute_per_ip: Optional[int] = None
    daily_limit: Optional[int] = None
    daily_limit_per_ip: Optional[int] = None

    # --- Captcha (one provider per client) ---
    captcha_provider: Optional[CaptchaProvider] = None
    captcha_secret: Optional[str] = None

    # --- Backwards-compat: turnstile_secret was the original field ---
    turnstile_secret: Optional[str] = None

    @field_validator("api_key")
    @classmethod
    def _check_key_prefix(cls, v: str) -> str:
        if not v.startswith("mg_"):
            raise ValueError("api_key must start with 'mg_' (mailgate prefix)")
        return v

    @model_validator(mode="after")
    def _migrate_turnstile_field(self) -> "ClientConfig":
        # Promote legacy turnstile_secret to the new captcha_provider/secret pair.
        if self.turnstile_secret and not self.captcha_secret:
            self.captcha_provider = "turnstile"
            self.captcha_secret = self.turnstile_secret
        if self.captcha_provider and not self.captcha_secret:
            raise ValueError("captcha_provider set without captcha_secret")
        return self


class Settings(BaseSettings):
    """Server-wide settings loaded from env vars (prefix MAILGATE_)."""

    model_config = SettingsConfigDict(
        env_prefix="MAILGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- SMTP ---
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    smtp_use_tls: bool = True
    smtp_timeout: int = 15

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # --- Multi-tenant (optional): JSON file with client list ---
    clients_file: Optional[Path] = None

    # --- Single-tenant (env-only) client config ---
    client_name: str = "default"
    client_api_key: Optional[str] = None
    client_allowed_origins: str = ""
    client_allowed_from_addresses: str = ""
    client_allowed_to_addresses: str = ""
    client_default_from: Optional[str] = None
    client_default_to: str = ""
    client_subject_prefix: Optional[str] = None
    client_rate_limit_per_minute: int = 10
    client_rate_limit_per_minute_per_ip: Optional[int] = None
    client_daily_limit: Optional[int] = None
    client_daily_limit_per_ip: Optional[int] = None
    client_ip_blocklist: str = ""
    client_ip_allowlist: str = ""
    client_captcha_provider: Optional[CaptchaProvider] = None
    client_captcha_secret: Optional[str] = None
    client_turnstile_secret: Optional[str] = None  # legacy alias


def _client_from_env(s: Settings) -> Optional[ClientConfig]:
    if not s.client_api_key:
        return None
    return ClientConfig(
        name=s.client_name,
        api_key=s.client_api_key,
        allowed_origins=_split_csv(s.client_allowed_origins),
        allowed_from_addresses=_split_csv_or_none(s.client_allowed_from_addresses),
        allowed_to_addresses=_split_csv_or_none(s.client_allowed_to_addresses),
        default_from=s.client_default_from,
        default_to=_split_csv_or_none(s.client_default_to),
        subject_prefix=s.client_subject_prefix,
        rate_limit_per_minute=s.client_rate_limit_per_minute,
        rate_limit_per_minute_per_ip=s.client_rate_limit_per_minute_per_ip,
        daily_limit=s.client_daily_limit,
        daily_limit_per_ip=s.client_daily_limit_per_ip,
        ip_blocklist=_split_csv(s.client_ip_blocklist),
        ip_allowlist=_split_csv(s.client_ip_allowlist),
        captcha_provider=s.client_captcha_provider,
        captcha_secret=s.client_captcha_secret,
        turnstile_secret=s.client_turnstile_secret,
    )


def _clients_from_file(path: Path) -> list[ClientConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: must be a JSON array")
    return [ClientConfig.model_validate(c) for c in raw]


def load_clients(settings: Settings) -> dict[str, ClientConfig]:
    """Build the api_key -> ClientConfig map from env and/or clients_file.

    Either source alone is sufficient. Both can be combined for multi-tenant deploys
    that also want a default env-based client.
    """
    clients: list[ClientConfig] = []

    if settings.clients_file is not None and settings.clients_file.exists():
        clients.extend(_clients_from_file(settings.clients_file))

    env_client = _client_from_env(settings)
    if env_client is not None:
        clients.append(env_client)

    if not clients:
        raise RuntimeError(
            "No clients configured. Set MAILGATE_CLIENT_API_KEY (single-tenant env "
            "config) or MAILGATE_CLIENTS_FILE (path to clients.json)."
        )

    by_key = {c.api_key: c for c in clients}
    if len(by_key) != len(clients):
        raise ValueError("Duplicate api_key across config sources")
    return by_key
