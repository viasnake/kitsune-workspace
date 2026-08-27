"""Validated Workspace configuration."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from kitsune.logging import MANDATORY_REDACTED_KEYS, normalize_redacted_keys
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _StrictSection(BaseModel):
    """Reject misspelled or obsolete keys in every nested settings section."""

    model_config = ConfigDict(extra="forbid")


class WorkspaceSection(_StrictSection):
    """Core Workspace process and persistence settings."""

    name: str = "default"
    bind: str = "127.0.0.1:8080"
    database_url: str = "sqlite:///./.kitsune-workspace/workspace.sqlite3"
    agent_manifest_directory: Path = Path("./config/agents")
    runtime_state_directory: Path = Path("./.kitsune-workspace/runtime")
    static_directory: Path | None = None
    public_url: str | None = None
    agent_url: str | None = None

    @field_validator("bind")
    @classmethod
    def validate_bind(cls, value: str) -> str:
        """Require a concrete host and valid TCP port."""

        host, separator, port_text = value.rpartition(":")
        if not separator or not host:
            raise ValueError("bind must use HOST:PORT syntax")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("bind port must be between 1 and 65535")
        return value

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str | None) -> str | None:
        """Require a credential-free absolute Workspace base URL."""

        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("workspace.public_url must be an absolute HTTP(S) URL")
        if (
            parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "workspace.public_url must be a credential-free root URL without query or fragment"
            )
        return value.rstrip("/")

    @field_validator("agent_url")
    @classmethod
    def validate_agent_url(cls, value: str | None) -> str | None:
        """Require a root HTTP(S) egress URL dedicated to managed Agents."""

        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("workspace.agent_url must be an absolute HTTP(S) URL")
        if (
            parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "workspace.agent_url must be a credential-free root URL without query or fragment"
            )
        return value.rstrip("/")

    @property
    def bind_host(self) -> str:
        """Return the configured bind host."""

        return self.bind.rpartition(":")[0].strip("[]")

    @property
    def bind_port(self) -> int:
        """Return the configured bind port."""

        return int(self.bind.rpartition(":")[2])


class AuthSection(_StrictSection):
    """Human authentication and authorization settings."""

    mode: Literal["none", "oidc"] = "none"
    issuer: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    redirect_uri: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    role_claim: str = "roles"
    default_role: Literal["viewer", "operator", "admin"] = "viewer"
    session_secret: SecretStr | None = None
    session_cookie_name: str = "kitsune_session"
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    secure_cookie: bool = True

    @field_validator("client_secret", "session_secret", mode="before")
    @classmethod
    def resolve_oidc_secret(cls, value: Any) -> Any:
        """Resolve supported OIDC secret references without accepting literals."""

        if value is None:
            return None
        reference = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(reference, str) or not reference.startswith(("env://", "file://")):
            raise ValueError("OIDC secrets must use an env:// or file:// reference")
        return resolve_secret_reference(reference)

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret_strength(cls, value: SecretStr | None) -> SecretStr | None:
        """Require at least 32 bytes for locally signed session material."""

        if value is not None and len(value.get_secret_value().encode()) < 32:
            raise ValueError("OIDC session secret must contain at least 32 bytes")
        return value

    @field_validator("issuer", "redirect_uri")
    @classmethod
    def validate_oidc_https_url(cls, value: str | None) -> str | None:
        """Require absolute HTTPS OIDC endpoints without embedded credentials."""

        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("OIDC issuer and redirect_uri must use absolute HTTPS URLs")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("OIDC URLs cannot contain credentials or fragments")
        return value.rstrip("/") if parsed.query == "" else value

    @model_validator(mode="after")
    def validate_oidc(self) -> AuthSection:
        """Require all credentials and secure cookies when OIDC mode is active."""

        if self.mode == "oidc":
            missing = [
                name
                for name in (
                    "issuer",
                    "client_id",
                    "client_secret",
                    "redirect_uri",
                    "session_secret",
                )
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"OIDC configuration is missing: {', '.join(missing)}")
            if not self.secure_cookie:
                raise ValueError("OIDC session cookies must be secure")
        return self


class EventSection(_StrictSection):
    """Event and payload persistence limits."""

    retention_days: int = Field(default=30, ge=1)
    audit_retention_days: int = Field(default=365, ge=1)
    runtime_retention_days: int = Field(default=90, ge=1)
    max_payload_bytes: int = Field(default=1_048_576, ge=1024, le=268_435_456)
    max_input_bytes: int = Field(default=1_048_576, ge=1024, le=268_435_456)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=268_435_456)
    max_runs_per_agent: int = Field(default=10_000, ge=1, le=1_000_000)
    max_events_per_agent: int = Field(default=100_000, ge=1, le=10_000_000)
    max_runtime_instances_per_agent: int = Field(default=1_000, ge=1, le=100_000)
    max_active_runtime_instances_per_agent: int = Field(default=5, ge=1, le=100)
    max_events_per_run: int = Field(default=10_000, ge=1, le=1_000_000)
    max_reserved_json_bytes_per_agent: int = Field(default=268_435_456, ge=1024, le=4_294_967_296)
    max_run_timeout_seconds: int = Field(default=86_400, ge=1, le=604_800)
    retention_batch_size: int = Field(default=500, ge=1, le=5_000)
    retention_batch_bytes: int = Field(default=8_388_608, ge=1024, le=268_435_456)

    @model_validator(mode="after")
    def validate_retained_storage_limits(self) -> EventSection:
        """Keep subordinate counts and object limits within each Agent's budget."""

        if self.max_events_per_run > self.max_events_per_agent:
            raise ValueError("events.max_events_per_run cannot exceed max_events_per_agent")
        if self.max_active_runtime_instances_per_agent > self.max_runtime_instances_per_agent:
            raise ValueError(
                "events.max_active_runtime_instances_per_agent cannot exceed "
                "max_runtime_instances_per_agent"
            )
        for name in ("max_payload_bytes", "max_input_bytes", "max_output_bytes"):
            if getattr(self, name) > self.max_reserved_json_bytes_per_agent:
                raise ValueError(f"events.{name} cannot exceed max_reserved_json_bytes_per_agent")
        return self


class SchedulerSection(_StrictSection):
    """Scheduler, dispatch, timeout, and retention loop settings."""

    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    heartbeat_timeout_seconds: int = Field(default=90, ge=5)
    lock_ttl_seconds: int = Field(default=30, ge=5)
    cancel_grace_seconds: int = Field(default=15, ge=1)
    retention_interval_seconds: int = Field(default=3600, ge=10)
    runtime_startup_timeout_seconds: int = Field(default=120, ge=5, le=3_600)
    runtime_probe_batch_size: int = Field(default=100, ge=1, le=1_000)
    runtime_probe_concurrency: int = Field(default=10, ge=1, le=100)
    runtime_probe_cycle_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    runtime_probe_backoff_max_seconds: float = Field(default=60.0, ge=1, le=3600)


class SecuritySection(_StrictSection):
    """Network and request boundary settings."""

    allow_insecure_external_agents: bool = False
    allow_insecure_agent_network: bool = False
    docker_socket: Path = Path("/var/run/docker.sock")
    managed_outbox_max_bytes: int = Field(default=67_108_864, ge=1024, le=1_073_741_824)
    docker_outbox_archive_max_bytes: int = Field(default=285_212_672, ge=1024, le=4_294_967_296)
    docker_outbox_archive_max_members: int = Field(default=64, ge=3, le=1024)
    docker_outbox_member_max_bytes: int = Field(default=134_217_728, ge=1024, le=4_294_967_296)
    docker_outbox_total_max_bytes: int = Field(default=268_435_456, ge=1024, le=4_294_967_296)
    docker_log_response_max_bytes: int = Field(default=2_097_152, ge=1024, le=268_435_456)
    docker_archived_logs_max_bytes: int = Field(default=33_554_432, ge=1024, le=1_073_741_824)
    docker_archived_log_containers: int = Field(default=100, ge=1, le=10_000)
    process_log_line_max_bytes: int = Field(default=65_536, ge=128, le=16_777_216)
    process_live_logs_max_bytes: int = Field(default=2_097_152, ge=128, le=268_435_456)
    process_archived_logs_max_bytes: int = Field(default=33_554_432, ge=128, le=1_073_741_824)
    process_archived_log_instances: int = Field(default=100, ge=1, le=10_000)
    webhook_rate_limit: int = Field(default=60, ge=1)
    api_rate_limit: int = Field(default=600, ge=1)
    sse_max_connections: int = Field(default=100, ge=1, le=10_000)
    sse_max_connections_per_principal: int = Field(default=5, ge=1, le=1_000)
    sse_max_connections_per_ip: int = Field(default=10, ge=1, le=1_000)
    redacted_keys: set[str] = Field(default_factory=lambda: set(MANDATORY_REDACTED_KEYS))

    @field_validator("redacted_keys")
    @classmethod
    def retain_mandatory_redactions(cls, value: set[str]) -> set[str]:
        """Treat configured keys as additions to the non-disableable security baseline."""

        return set(normalize_redacted_keys(frozenset(value)))

    @model_validator(mode="after")
    def validate_docker_response_limits(self) -> SecuritySection:
        """Keep per-member limits within the total extracted Outbox budget."""

        if self.docker_outbox_member_max_bytes > self.docker_outbox_total_max_bytes:
            raise ValueError(
                "security.docker_outbox_member_max_bytes cannot exceed "
                "docker_outbox_total_max_bytes"
            )
        if self.docker_outbox_member_max_bytes < self.managed_outbox_max_bytes * 2:
            raise ValueError(
                "security.docker_outbox_member_max_bytes must be at least twice "
                "managed_outbox_max_bytes"
            )
        if self.docker_outbox_total_max_bytes < self.managed_outbox_max_bytes * 4:
            raise ValueError(
                "security.docker_outbox_total_max_bytes must be at least four times "
                "managed_outbox_max_bytes"
            )
        if self.docker_outbox_archive_max_bytes < self.docker_outbox_total_max_bytes:
            raise ValueError(
                "security.docker_outbox_archive_max_bytes cannot be lower than "
                "docker_outbox_total_max_bytes"
            )
        return self

    @model_validator(mode="after")
    def validate_sse_connection_limits(self) -> SecuritySection:
        """Keep identity-specific SSE limits within the global connection cap."""

        if self.sse_max_connections_per_principal > self.sse_max_connections:
            raise ValueError("per-principal SSE limit cannot exceed the global limit")
        if self.sse_max_connections_per_ip > self.sse_max_connections:
            raise ValueError("per-IP SSE limit cannot exceed the global limit")
        return self

    @model_validator(mode="after")
    def validate_process_log_limits(self) -> SecuritySection:
        """Keep one captured process line within its per-instance live budget."""

        if self.process_log_line_max_bytes > self.process_live_logs_max_bytes:
            raise ValueError(
                "security.process_log_line_max_bytes cannot exceed process_live_logs_max_bytes"
            )
        return self


class ObservabilitySection(_StrictSection):
    """Workspace telemetry identity."""

    service_name: str = "kitsune-workspace"
    otlp_endpoint: str | None = None


class WorkspaceSettings(BaseSettings):
    """Complete, validated settings for one active Workspace process."""

    model_config = SettingsConfigDict(
        env_prefix="KITSUNE_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    workspace: WorkspaceSection = Field(default_factory=WorkspaceSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    events: EventSection = Field(default_factory=EventSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)

    @model_validator(mode="after")
    def validate_trust_boundaries(self) -> WorkspaceSettings:
        """Prevent unauthenticated Workspace APIs from binding beyond loopback."""

        if self.auth.mode == "none":
            host = self.workspace.bind_host
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                if host != "localhost":
                    raise ValueError("auth.mode=none requires a loopback bind address") from exc
            else:
                if not address.is_loopback:
                    raise ValueError("auth.mode=none requires a loopback bind address")
            if self.workspace.public_url is not None:
                public_host = urlparse(self.workspace.public_url).hostname or ""
                try:
                    public_address = ipaddress.ip_address(public_host)
                except ValueError as exc:
                    if public_host.casefold() != "localhost":
                        raise ValueError(
                            "auth.mode=none requires a loopback workspace.public_url"
                        ) from exc
                else:
                    if not public_address.is_loopback:
                        raise ValueError("auth.mode=none requires a loopback workspace.public_url")
        elif (
            self.workspace.public_url is None
            or urlparse(self.workspace.public_url).scheme != "https"
        ):
            raise ValueError("auth.mode=oidc requires an HTTPS workspace.public_url")
        if self.workspace.agent_url is not None:
            parsed_agent_url = urlparse(self.workspace.agent_url)
            agent_host = parsed_agent_url.hostname or ""
            try:
                agent_url_is_loopback = ipaddress.ip_address(agent_host).is_loopback
            except ValueError:
                agent_url_is_loopback = agent_host.casefold() == "localhost"
            if (
                parsed_agent_url.scheme == "http"
                and not agent_url_is_loopback
                and not self.security.allow_insecure_agent_network
            ):
                raise ValueError(
                    "non-loopback HTTP workspace.agent_url requires "
                    "security.allow_insecure_agent_network=true"
                )
        return self

    @classmethod
    def from_toml(cls, path: str | Path) -> WorkspaceSettings:
        """Load and validate a TOML settings file, resolving secret references."""

        config_path = Path(path)
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
        auth = raw.get("auth")
        if isinstance(auth, dict):
            if auth.get("mode") == "oidc":
                for key in ("client_secret", "session_secret"):
                    reference = auth.get(key)
                    if not isinstance(reference, str) or not reference.startswith(
                        ("env://", "file://")
                    ):
                        raise ValueError(f"auth.{key} must use an env:// or file:// reference")
        return cls.model_validate(raw)

    @classmethod
    def load(cls, path: str | Path | None = None) -> WorkspaceSettings:
        """Load a named TOML file or environment-only configuration."""

        selected = path or os.getenv("KITSUNE_WORKSPACE_CONFIG")
        return cls.from_toml(selected) if selected else cls()


def resolve_secret_reference(reference: str) -> str:
    """Resolve an ``env://`` or absolute ``file://`` secret reference."""

    if reference.startswith("env://"):
        variable = reference.removeprefix("env://")
        if not variable or variable not in os.environ:
            raise ValueError(f"environment secret {variable!r} is not set")
        value = os.environ[variable]
        if not value.strip():
            raise ValueError(f"environment secret {variable!r} is empty")
        return value
    if reference.startswith("file://"):
        secret_path = Path(reference.removeprefix("file://"))
        if not secret_path.is_absolute():
            raise ValueError("file secret references must use an absolute path")
        value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
        if not value.strip():
            raise ValueError(f"file secret {secret_path} is empty")
        return value
    return reference


def as_plain_dict(model: BaseModel) -> dict[str, Any]:
    """Return a JSON-compatible representation for persistence."""

    return model.model_dump(mode="json", exclude_none=True)
