"""Pydantic models shared by the Kitsune SDK and Workspace."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_OBSERVABILITY_TEMPLATE_FIELDS = frozenset(
    {"agent_id", "runtime_instance_id", "run_id", "correlation_id", "trace_id"}
)


def _validate_docker_volume_mount(value: str) -> str:
    """Validate one unambiguous Linux Docker bind-mount string."""

    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(
            "Docker volume mounts must use absolute-host-path:absolute-container-path[:ro|rw]"
        )
    host_path, container_path = parts[:2]
    container_mount = PurePosixPath(container_path)
    if ".." in container_mount.parts:
        raise ValueError("Docker volume container paths must not contain dot-dot segments")
    if not Path(host_path).is_absolute() or not Path(container_path).is_absolute():
        raise ValueError("Docker volume mount host and container paths must be absolute")
    if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
        raise ValueError("Docker volume mount mode must be ro or rw")
    docker_socket = Path("/var/run/docker.sock").resolve(strict=False)
    resolved_source = Path(host_path).resolve(strict=False)
    if (
        resolved_source == docker_socket
        or resolved_source in docker_socket.parents
        or container_mount == PurePosixPath("/var/run/docker.sock")
    ):
        raise ValueError("Docker volumes must not expose the Docker Engine socket")
    managed_outbox = PurePosixPath("/var/lib/kitsune-outbox")
    if (
        container_mount == managed_outbox
        or container_mount in managed_outbox.parents
        or managed_outbox in container_mount.parents
    ):
        raise ValueError("Docker volumes must not overlap the Workspace-managed outbox mount")
    return value


def _validate_agent_control_url(value: AnyHttpUrl) -> AnyHttpUrl:
    """Validate one canonical root base URL for an Agent Control API."""

    if value.username is not None or value.password is not None:
        raise ValueError("Agent Control URLs must not contain user information")
    if value.query is not None or value.fragment is not None:
        raise ValueError("Agent Control URLs must not contain a query or fragment")
    if value.path not in {None, "", "/"}:
        raise ValueError("Agent Control URLs must use an empty or root path")
    return value


def _validate_observability_url_template(value: str) -> str:
    """Validate one restricted absolute HTTP(S) operator-link template."""

    if not value:
        return value
    if value != value.strip():
        raise ValueError("Observability URL templates must not have surrounding whitespace")
    test_values = {field: "value" for field in _OBSERVABILITY_TEMPLATE_FIELDS}
    rendered = _format_observability_url_template(value, test_values)
    _validate_observability_url(rendered)
    return value


def _format_observability_url_template(template: str, values: dict[str, str]) -> str:
    try:
        fields = Formatter().parse(template)
        for _, field_name, format_spec, conversion in fields:
            if field_name is None:
                continue
            if (
                field_name not in _OBSERVABILITY_TEMPLATE_FIELDS
                or format_spec
                or conversion is not None
            ):
                raise ValueError(f"unsupported observability template field: {field_name!r}")
        return template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid observability URL template: {exc}") from exc


def _validate_observability_url(value: str) -> AnyHttpUrl:
    url = _HTTP_URL_ADAPTER.validate_python(value)
    if url.username is not None or url.password is not None:
        raise ValueError("Observability URLs must not contain user information")
    if url.fragment is not None:
        raise ValueError("Observability URLs must not contain a fragment")
    return url


AgentId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    ),
]
HandlerName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
    ),
]
AgentControlUrl = Annotated[AnyHttpUrl, AfterValidator(_validate_agent_control_url)]
DockerVolumeMount = Annotated[
    str,
    StringConstraints(max_length=4096),
    AfterValidator(_validate_docker_volume_mount),
]
DockerCommandPart = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
ObservabilityUrlTemplate = Annotated[str, AfterValidator(_validate_observability_url_template)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeBigInt = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
JsonObject = dict[str, Any]

AGENT_DESCRIPTOR_MAX_BYTES = 1_048_576
AGENT_DESCRIPTOR_MAX_ITEMS = 1_000
MAX_RUN_TIMEOUT_SECONDS = 604_800
_DOCKER_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DOCKER_RESERVED_NETWORKS = frozenset({"bridge", "default", "host", "none"})


def render_observability_url(
    template: ObservabilityUrlTemplate,
    *,
    agent_id: str,
    runtime_instance_id: str | UUID | None = None,
    run_id: str | UUID | None = None,
    correlation_id: str | UUID | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Render a validated trace or log link with URL-escaped identifier values."""

    validated_template = _validate_observability_url_template(template)
    if not validated_template:
        return None
    raw_values: dict[str, str | UUID | None] = {
        "agent_id": agent_id,
        "runtime_instance_id": runtime_instance_id,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    values = {
        field: quote(str(value), safe="") if value is not None else ""
        for field, value in raw_values.items()
    }
    rendered = _format_observability_url_template(validated_template, values)
    return str(_validate_observability_url(rendered))


class ContractModel(BaseModel):
    """Base class that rejects unknown contract fields."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RuntimeMode(StrEnum):
    """Lifetime pattern of an Agent Application process."""

    RESIDENT = "resident"
    EPHEMERAL = "ephemeral"


class RuntimeAdapter(StrEnum):
    """Mechanism used to start or register a Runtime Instance."""

    PROCESS = "process"
    DOCKER = "docker"
    EXTERNAL = "external"


class DesiredState(StrEnum):
    """Operator-requested lifecycle state for an Agent Definition."""

    RUNNING = "running"
    STOPPED = "stopped"


class RestartPolicy(StrEnum):
    """Policy applied when a managed Runtime Instance exits."""

    NEVER = "never"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class RuntimeStatus(StrEnum):
    """Observed lifecycle state of one Runtime Instance."""

    PENDING = "pending"
    STARTING = "starting"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    LOST = "lost"


class RunStatus(StrEnum):
    """Lifecycle state of one Run."""

    CREATED = "created"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        """Return whether this status is a terminal Run outcome."""

        return self in TERMINAL_RUN_STATUSES


class RunSource(StrEnum):
    """Immediate cause that created a Run."""

    ON_DEMAND = "on_demand"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    SELF = "self"
    CHILD = "child"


def _validate_agent_run_lineage(source: RunSource, parent_run_id: UUID | None) -> None:
    if source is RunSource.CHILD and parent_run_id is None:
        raise ValueError("child Agent Runs require parent_run_id")
    if source is not RunSource.CHILD and parent_run_id is not None:
        raise ValueError("only child Agent Runs may contain parent_run_id")


class TriggerType(StrEnum):
    """Manifest trigger type that can create a Run."""

    ON_DEMAND = "on_demand"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    SELF = "self"
    CHILD = "child"


class QueuePolicy(StrEnum):
    """Behavior when all Handler concurrency slots are occupied."""

    QUEUE = "queue"
    REJECT = "reject"


class OverlapPolicy(StrEnum):
    """Behavior when a schedule fires while an earlier Run is active."""

    ALLOW = "allow"
    SKIP = "skip"
    QUEUE = "queue"
    REPLACE = "replace"


class EventSeverity(StrEnum):
    """Severity attached to a Kitsune Event."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RestartConfiguration(ContractModel):
    """Restart and crash-loop settings for a managed Runtime Instance."""

    policy: RestartPolicy = RestartPolicy.ON_FAILURE
    max_attempts: int = Field(default=5, ge=0, le=1_000)
    backoff_seconds: float = Field(default=5, ge=0, le=MAX_RUN_TIMEOUT_SECONDS, allow_inf_nan=False)
    max_backoff_seconds: float = Field(
        default=300, ge=0, le=MAX_RUN_TIMEOUT_SECONDS, allow_inf_nan=False
    )
    reset_after_seconds: float = Field(
        default=300, gt=0, le=MAX_RUN_TIMEOUT_SECONDS, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_backoff(self) -> Self:
        """Ensure the maximum backoff is not lower than its initial value."""

        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least backoff_seconds")
        return self


class ProcessRuntimeConfiguration(ContractModel):
    """Manifest-declared command for the Process Runtime Adapter."""

    command: list[str] = Field(min_length=1)
    control_url: AgentControlUrl | None = None
    working_directory: Path | None = None

    @field_validator("command")
    @classmethod
    def validate_command_parts(cls, value: list[str]) -> list[str]:
        """Reject empty process command elements."""

        if any(not part.strip() for part in value):
            raise ValueError("process command elements must not be empty")
        return value


class DockerRuntimeConfiguration(ContractModel):
    """Manifest-declared container settings for the Docker Runtime Adapter."""

    image: str = Field(min_length=1, max_length=255)
    control_port: int | None = Field(default=None, ge=1024, le=65535)
    command: list[DockerCommandPart] | None = Field(default=None, min_length=1, max_length=256)
    working_directory: str | None = Field(default=None, max_length=4096)
    network: str
    volumes: list[DockerVolumeMount] = Field(default_factory=list, max_length=64)
    memory_limit_bytes: int = Field(default=536_870_912, ge=67_108_864, le=68_719_476_736)
    cpu_limit: float = Field(default=1.0, ge=0.01, le=64, allow_inf_nan=False)
    pids_limit: int = Field(default=256, ge=16, le=32_768)
    privileged: bool = False
    host_network: bool = False

    @field_validator("network")
    @classmethod
    def validate_isolated_network(cls, value: str) -> str:
        """Require one explicit operator-created network name, never namespace sharing."""

        network = value.strip()
        folded = network.casefold()
        if (
            not network
            or len(network) > 128
            or folded in _DOCKER_RESERVED_NETWORKS
            or folded.startswith(("container:", "service:"))
            or ":" in network
            or "/" in network
            or _DOCKER_NETWORK_NAME.fullmatch(network) is None
        ):
            raise ValueError("Docker network must name an explicit isolated bridge network")
        return network

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str | None) -> str | None:
        """Require Docker's WorkingDir to be one normalized absolute POSIX path."""

        if value is None:
            return None
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or value.startswith("//")
            or ".." in path.parts
            or path.as_posix() != value
        ):
            raise ValueError("Docker working_directory must be a normalized absolute POSIX path")
        return value

    @model_validator(mode="after")
    def prohibit_privilege_escalation(self) -> Self:
        """Reject privileged containers and host networking in a manifest."""

        if self.privileged:
            raise ValueError("privileged Docker containers are prohibited")
        if self.host_network:
            raise ValueError("Docker host networking is prohibited")
        return self


class ExternalRuntimeConfiguration(ContractModel):
    """Endpoint settings for a Runtime Instance managed outside Workspace."""

    endpoint: AgentControlUrl | None = None
    heartbeat_timeout_seconds: int = Field(default=60, gt=0, le=MAX_RUN_TIMEOUT_SECONDS)


class RuntimeConfiguration(ContractModel):
    """Desired Runtime Adapter, lifetime, and launch configuration."""

    adapter: RuntimeAdapter
    mode: RuntimeMode
    desired_state: DesiredState = DesiredState.RUNNING
    restart: RestartConfiguration = Field(default_factory=RestartConfiguration)
    process: ProcessRuntimeConfiguration | None = None
    docker: DockerRuntimeConfiguration | None = None
    external: ExternalRuntimeConfiguration | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("environment")
    @classmethod
    def validate_environment_references(cls, value: dict[str, str]) -> dict[str, str]:
        """Require environment values to use supported secret reference schemes."""

        for key, reference in value.items():
            _validate_environment_name(key)
            _validate_reference(reference)
        return value

    @field_validator("secrets")
    @classmethod
    def validate_secret_references(cls, value: dict[str, str]) -> dict[str, str]:
        """Require secrets to use env or absolute file references."""

        for key, reference in value.items():
            _validate_environment_name(key)
            _validate_reference(reference)
        return value

    @model_validator(mode="after")
    def validate_adapter_configuration(self) -> Self:
        """Require exactly the configuration selected by ``adapter``."""

        selected = {
            RuntimeAdapter.PROCESS: self.process,
            RuntimeAdapter.DOCKER: self.docker,
            RuntimeAdapter.EXTERNAL: self.external,
        }
        if selected[self.adapter] is None:
            raise ValueError(
                f"runtime.{self.adapter.value} is required for adapter {self.adapter.value}"
            )
        configured = [adapter for adapter, config in selected.items() if config is not None]
        if configured != [self.adapter]:
            names = ", ".join(adapter.value for adapter in configured)
            raise ValueError(
                f"runtime configuration must only contain {self.adapter.value}; got {names}"
            )
        if self.mode is RuntimeMode.RESIDENT:
            if self.adapter is RuntimeAdapter.PROCESS:
                assert self.process is not None
                if self.process.control_url is None:
                    raise ValueError("resident Process Runtime requires process.control_url")
            if self.adapter is RuntimeAdapter.DOCKER:
                assert self.docker is not None
                if self.docker.control_port is None:
                    raise ValueError("resident Docker Runtime requires docker.control_port")
            if self.adapter is RuntimeAdapter.EXTERNAL:
                assert self.external is not None
                if self.external.endpoint is None:
                    raise ValueError("resident External Runtime requires external.endpoint")
        elif self.adapter is RuntimeAdapter.DOCKER:
            assert self.docker is not None
            if self.docker.control_port is not None:
                raise ValueError("ephemeral Docker Runtime cannot declare docker.control_port")
        return self


class HandlerInvocationConfiguration(ContractModel):
    """Optional manifest admission overrides for one named Handler."""

    max_concurrency: int | None = Field(default=None, gt=0, le=1_000_000)
    queue_capacity: int | None = Field(default=None, ge=0, le=1_000_000)
    queue_policy: QueuePolicy | None = None


class InvocationConfiguration(ContractModel):
    """Agent defaults and per-Handler overrides for Run admission and retention."""

    default_handler: HandlerName
    max_concurrency: int = Field(default=1, gt=0, le=1_000_000)
    queue_capacity: int = Field(default=0, ge=0, le=1_000_000)
    queue_policy: QueuePolicy = QueuePolicy.QUEUE
    timeout_seconds: int = Field(default=900, gt=0, le=MAX_RUN_TIMEOUT_SECONDS)
    cancellation_grace_seconds: float = Field(
        default=10, ge=0, le=MAX_RUN_TIMEOUT_SECONDS, allow_inf_nan=False
    )
    store_input: bool = True
    store_output: bool = True
    retention_days: int = Field(default=30, gt=0, le=36_500)
    handlers: dict[HandlerName, HandlerInvocationConfiguration] = Field(
        default_factory=dict, max_length=AGENT_DESCRIPTOR_MAX_ITEMS
    )


class TriggerDefinition(ContractModel):
    """Manifest-declared cause that may create a Run."""

    id: Annotated[
        str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    ]
    type: TriggerType
    handler: HandlerName
    cron: str | None = Field(default=None, max_length=255)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    overlap: OverlapPolicy = OverlapPolicy.SKIP
    misfire_grace_seconds: int = Field(default=300, ge=0, le=MAX_RUN_TIMEOUT_SECONDS)
    shared_secret_ref: str | None = None
    hmac_secret_ref: str | None = None
    hmac_header: str = "X-Kitsune-Signature"
    idempotency_header: str = "Idempotency-Key"
    max_request_bytes: int = Field(default=1_048_576, gt=0, le=268_435_456)
    rate_limit_per_minute: int = Field(default=60, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> Self:
        """Validate fields that only have meaning for schedule or webhook triggers."""

        if self.type is TriggerType.SCHEDULE:
            if not self.cron or len(self.cron.split()) != 5:
                raise ValueError("schedule triggers require a five-field cron expression")
        elif self.cron is not None:
            raise ValueError("cron is only valid for schedule triggers")
        if self.type is TriggerType.WEBHOOK:
            if self.shared_secret_ref is None and self.hmac_secret_ref is None:
                raise ValueError("webhook triggers require shared_secret_ref or hmac_secret_ref")
            for reference in (self.shared_secret_ref, self.hmac_secret_ref):
                if reference is not None:
                    _validate_reference(reference)
        elif self.shared_secret_ref is not None or self.hmac_secret_ref is not None:
            raise ValueError("webhook secret references are only valid for webhook triggers")
        return self


class ObservabilityConfiguration(ContractModel):
    """Service identity and operator links for logs and traces."""

    service_name: str = Field(min_length=1)
    trace_url_template: ObservabilityUrlTemplate = ""
    log_url_template: ObservabilityUrlTemplate = ""


class SecurityConfiguration(ContractModel):
    """Agent authentication settings held by an Agent Definition."""

    agent_token_ref: str

    @field_validator("agent_token_ref")
    @classmethod
    def validate_agent_token_reference(cls, value: str) -> str:
        """Require a supported reference rather than a literal token."""

        _validate_reference(value)
        return value


class AgentMetadata(ContractModel):
    """Human-readable metadata for an Agent Definition."""

    id: AgentId
    display_name: str = Field(min_length=1, max_length=255)
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class AgentSpecification(ContractModel):
    """Desired runtime, invocation, trigger, observability, and security state."""

    runtime: RuntimeConfiguration
    invocation: InvocationConfiguration
    triggers: list[TriggerDefinition] = Field(
        default_factory=list, max_length=AGENT_DESCRIPTOR_MAX_ITEMS
    )
    observability: ObservabilityConfiguration
    security: SecurityConfiguration

    @model_validator(mode="after")
    def validate_unique_triggers(self) -> Self:
        """Reject duplicate trigger IDs."""

        ids = [trigger.id for trigger in self.triggers]
        if len(ids) != len(set(ids)):
            raise ValueError("trigger ids must be unique")
        return self


class AgentManifest(ContractModel):
    """Source-of-truth declaration for one logical Agent Application."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True, serialize_by_alias=True
    )

    schema_name: Literal["kitsune.agent"] = Field(
        default="kitsune.agent", validation_alias="schema", serialization_alias="schema"
    )
    revision: Literal[1] = 1
    metadata: AgentMetadata
    spec: AgentSpecification

    @property
    def schema(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> Literal["kitsune.agent"]:
        """Return the public contract identifier represented by the ``schema`` alias."""

        return self.schema_name


class HandlerDescriptor(ContractModel):
    """Typed invocation entry point reported by a running SDK."""

    name: HandlerName
    description: str = Field(default="", max_length=4096)
    input_schema: JsonObject
    output_schema: JsonObject
    default_timeout_seconds: int = Field(default=900, gt=0, le=MAX_RUN_TIMEOUT_SECONDS)


class PluginDescriptor(ContractModel):
    """Plugin identity reported in an Agent Descriptor."""

    name: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)


class AgentDescriptor(ContractModel):
    """Capabilities and build identity reported by one running SDK process."""

    agent_id: AgentId
    application_version: str = Field(min_length=1, max_length=255)
    sdk_version: str = Field(min_length=1, max_length=255)
    framework: str = Field(default="generic", max_length=255)
    build_revision: str | None = Field(default=None, max_length=255)
    handlers: list[HandlerDescriptor] = Field(max_length=AGENT_DESCRIPTOR_MAX_ITEMS)
    plugins: list[PluginDescriptor] = Field(
        default_factory=list, max_length=AGENT_DESCRIPTOR_MAX_ITEMS
    )
    started_at: datetime
    capabilities: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_handlers_and_plugins(self) -> Self:
        """Reject ambiguous duplicate handler and plugin reports."""

        handlers = [handler.name for handler in self.handlers]
        plugins = [plugin.name for plugin in self.plugins]
        if len(handlers) != len(set(handlers)):
            raise ValueError("handler names must be unique")
        if len(plugins) != len(set(plugins)):
            raise ValueError("plugin names must be unique")
        return self


class UsageRecord(ContractModel):
    """Bounded model usage for one operation; unavailable values remain ``None``."""

    provider: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    request_count: NonNegativeBigInt | None = None
    input_tokens: NonNegativeBigInt | None = None
    output_tokens: NonNegativeBigInt | None = None
    total_tokens: NonNegativeBigInt | None = None
    cache_read_tokens: NonNegativeBigInt | None = None
    cache_write_tokens: NonNegativeBigInt | None = None
    estimated_cost: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=38,
        decimal_places=18,
        allow_inf_nan=False,
    )
    currency: str | None = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_cost_currency(self) -> Self:
        """Require currency for cost and keep every valid Usage Event durably queueable."""

        if self.estimated_cost is not None and not self.currency:
            raise ValueError("currency is required when estimated_cost is present")
        payload = self.model_dump(mode="json")
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > 768:
            raise ValueError("serialized Usage Record must not exceed 768 bytes")
        return self


class RunError(ContractModel):
    """Sanitized typed error recorded for a failed Run."""

    type: str
    message: str
    retryable: bool = False
    details: JsonObject = Field(default_factory=dict)


class RunRecord(ContractModel):
    """Persistent record of one processing request and its terminal outcome."""

    run_id: UUID = Field(default_factory=uuid4)
    agent_id: AgentId
    runtime_instance_id: UUID | None = None
    handler: HandlerName
    source: RunSource
    parent_run_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,32}$")
    status: RunStatus = RunStatus.CREATED
    input: Any = None
    output: Any = None
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: RunError | None = None
    usage: list[UsageRecord] = Field(default_factory=list[UsageRecord])

    @model_validator(mode="after")
    def validate_timeline_and_outcome(self) -> Self:
        """Keep timestamps and terminal fields consistent with Run state."""

        _validate_agent_run_lineage(self.source, self.parent_run_id)
        ordered = [
            stamp
            for stamp in (self.created_at, self.queued_at, self.started_at, self.ended_at)
            if stamp
        ]
        if ordered != sorted(ordered):
            raise ValueError("run timestamps must be chronological")
        if self.status.terminal and self.ended_at is None:
            raise ValueError("terminal runs require ended_at")
        if not self.status.terminal and self.ended_at is not None:
            raise ValueError("non-terminal runs must not have ended_at")
        if self.status is RunStatus.FAILED and self.error is None:
            raise ValueError("failed runs require error")
        if self.error is not None and self.status not in {RunStatus.FAILED, RunStatus.TIMED_OUT}:
            raise ValueError("only failed or timed-out runs may contain error")
        return self


class KitsuneEvent(ContractModel):
    """Immutable operational fact delivered at least once by event ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    type: str = Field(pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$", max_length=256)
    occurred_at: datetime
    agent_id: AgentId
    runtime_instance_id: UUID | None = None
    run_id: UUID | None = None
    parent_run_id: UUID | None = None
    correlation_id: UUID | None = None
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,32}$")
    severity: EventSeverity = EventSeverity.INFO
    payload: JsonObject = Field(default_factory=dict)


class RunOutcome(ContractModel):
    """Terminal result passed to Plugin run-finished hooks."""

    status: Literal[
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    ]
    output: Any = None
    error: RunError | None = None
    usage: list[UsageRecord] = Field(default_factory=list[UsageRecord])
    ended_at: datetime


class EventBatch(ContractModel):
    """At-least-once event delivery request sent by an SDK."""

    events: list[KitsuneEvent] = Field(min_length=1, max_length=500)


class AgentRegistration(ContractModel):
    """Runtime registration request sent by a resident Agent Application."""

    descriptor: AgentDescriptor
    runtime_instance_id: UUID
    control_url: AgentControlUrl | None = None


class AgentHeartbeat(ContractModel):
    """Best-effort liveness report for one Runtime Instance."""

    agent_id: AgentId
    runtime_instance_id: UUID
    occurred_at: datetime
    status: RuntimeStatus = RuntimeStatus.READY
    active_runs: NonNegativeInt = 0


class AgentRunBegin(ContractModel):
    """Request from an Agent to persist a self-triggered or child Run.

    For a child Run, ``handler`` names the child operation and need not match a
    capability in the parent Agent's Handler Descriptor.
    """

    run_id: UUID = Field(default_factory=uuid4)
    agent_id: AgentId
    runtime_instance_id: UUID
    handler: HandlerName = Field(
        description="Registered self Handler or child operation name, according to source"
    )
    source: Literal[RunSource.SELF, RunSource.CHILD] = RunSource.SELF
    parent_run_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,32}$")
    input: Any = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, gt=0, le=MAX_RUN_TIMEOUT_SECONDS)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_source_lineage(self) -> Self:
        """Require parent lineage exactly for child Runs."""

        _validate_agent_run_lineage(self.source, self.parent_run_id)
        return self


class AgentRunAssignment(ContractModel):
    """Persisted input and lineage assigned to an ephemeral Agent process."""

    run_id: UUID
    agent_id: AgentId
    handler: HandlerName
    source: RunSource
    input: Any = None
    parent_run_id: UUID | None = None
    correlation_id: UUID
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,32}$")
    deadline: datetime | None = None

    @model_validator(mode="after")
    def validate_source_lineage(self) -> Self:
        """Require parent lineage exactly for child assignments."""

        _validate_agent_run_lineage(self.source, self.parent_run_id)
        return self


class AgentRunAcknowledgement(ContractModel):
    """Notice that an ephemeral Agent accepted its assigned Run."""

    runtime_instance_id: UUID
    status: Literal[RunStatus.RUNNING] = RunStatus.RUNNING


TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
)

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.QUEUED: frozenset(
        {RunStatus.DISPATCHING, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED}
    ),
    RunStatus.DISPATCHING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: TERMINAL_RUN_STATUSES,
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
}


class InvalidRunTransition(ValueError):
    """Raised when a Run attempts a prohibited state transition."""

    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid run transition: {current.value} -> {target.value}")


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    """Return whether ``current`` may transition directly to ``target``."""

    return target in ALLOWED_RUN_TRANSITIONS[current]


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Raise :class:`InvalidRunTransition` for a prohibited state transition."""

    if not can_transition(current, target):
        raise InvalidRunTransition(current, target)


def _validate_environment_name(value: str) -> None:
    if (
        not value
        or not value.replace("_", "A").isalnum()
        or value[0].isdigit()
        or value.upper() != value
    ):
        raise ValueError(f"invalid environment variable name: {value!r}")


def _validate_reference(reference: str) -> None:
    if reference.startswith("env://"):
        _validate_environment_name(reference.removeprefix("env://"))
        return
    if reference.startswith("file:///") and Path(reference.removeprefix("file://")).is_absolute():
        return
    raise ValueError("references must use env://VARIABLE_NAME or file:///absolute/path")
