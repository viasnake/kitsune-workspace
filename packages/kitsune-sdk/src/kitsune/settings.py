"""Validated SDK settings loaded from ``KITSUNE_`` environment variables."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

from kitsune_contracts import AgentControlUrl
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging import MANDATORY_REDACTED_KEYS, normalize_redacted_keys

DEFAULT_OUTBOX_MAX_BYTES = 67_108_864


def validate_workspace_transport(url: str, *, allow_insecure: bool) -> None:
    """Reject bearer-token destinations outside the configured TLS trust boundary."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("KITSUNE_WORKSPACE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("KITSUNE_WORKSPACE_URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "https":
        return
    host = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == "localhost"
    if not loopback and not allow_insecure:
        raise ValueError(
            "HTTP KITSUNE_WORKSPACE_URL requires "
            "KITSUNE_ALLOW_INSECURE_WORKSPACE=true outside loopback"
        )


class KitsuneSettings(BaseSettings):
    """Runtime settings shared by resident and ephemeral SDK execution."""

    model_config = SettingsConfigDict(
        env_prefix="KITSUNE_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    workspace_url: AnyHttpUrl | None = None
    allow_insecure_workspace: bool = False
    agent_token: SecretStr | None = None
    runtime_instance_id: UUID = Field(default_factory=uuid4)
    control_url: AgentControlUrl | None = None
    outbox_path: Path = Path(".kitsune/events.sqlite3")
    outbox_capacity: int = Field(default=10_000, gt=0)
    outbox_max_bytes: int = Field(default=DEFAULT_OUTBOX_MAX_BYTES, ge=1024, le=4_294_967_296)
    event_batch_size: int = Field(default=100, gt=0, le=500)
    event_max_payload_bytes: int = Field(default=1_048_576, ge=1024)
    event_max_batch_bytes: int = Field(default=1_114_112, ge=1024)
    event_retry_initial_seconds: float = Field(default=0.5, gt=0)
    event_retry_max_seconds: float = Field(default=60, gt=0)
    heartbeat_interval_seconds: float = Field(default=15, gt=0)
    shutdown_grace_seconds: float = Field(default=30, ge=0)
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8081, ge=1, le=65535)
    control_max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    otlp_endpoint: AnyHttpUrl | None = Field(
        default=None,
        validation_alias=AliasChoices("KITSUNE_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    service_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KITSUNE_SERVICE_NAME", "OTEL_SERVICE_NAME"),
    )
    redacted_keys: frozenset[str] = MANDATORY_REDACTED_KEYS
    redacted_environment_variables: frozenset[str] = frozenset()

    @field_validator("redacted_keys")
    @classmethod
    def retain_mandatory_redactions(cls, value: frozenset[str]) -> frozenset[str]:
        """Treat configured keys as additions to the non-disableable security baseline."""

        return normalize_redacted_keys(value)

    @field_validator("agent_token")
    @classmethod
    def validate_agent_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject credentials that can compare equal to a missing Bearer value."""

        if value is not None and not value.get_secret_value().strip():
            raise ValueError("KITSUNE_AGENT_TOKEN must not be empty")
        return value

    @field_validator("redacted_environment_variables")
    @classmethod
    def validate_redacted_environment_variables(cls, value: frozenset[str]) -> frozenset[str]:
        """Require explicit portable environment names for concrete-value suppression."""

        invalid = sorted(
            name for name in value if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        )
        if invalid:
            raise ValueError("invalid redacted environment variable name: " + ", ".join(invalid))
        return value

    @model_validator(mode="after")
    def validate_managed_mode(self) -> KitsuneSettings:
        """Require credentials and valid retry bounds in managed mode."""

        if self.workspace_url is not None and self.agent_token is None:
            raise ValueError("KITSUNE_AGENT_TOKEN is required when KITSUNE_WORKSPACE_URL is set")
        if self.workspace_url is not None:
            validate_workspace_transport(
                str(self.workspace_url),
                allow_insecure=self.allow_insecure_workspace,
            )
        if self.event_retry_max_seconds < self.event_retry_initial_seconds:
            raise ValueError("event_retry_max_seconds must not be lower than the initial delay")
        if self.event_max_batch_bytes <= self.event_max_payload_bytes:
            raise ValueError("event_max_batch_bytes must be greater than event_max_payload_bytes")
        if self.event_max_batch_bytes > self.outbox_max_bytes:
            raise ValueError("event_max_batch_bytes cannot exceed outbox_max_bytes")
        return self

    @property
    def managed(self) -> bool:
        """Return whether this process is connected to Kitsune Workspace."""

        return self.workspace_url is not None
