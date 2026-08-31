"""Typed management and webhook API request and response models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from kitsune_contracts import (
    AgentDescriptor,
    AgentManifest,
    DesiredState,
    EventSeverity,
    OverlapPolicy,
    QueuePolicy,
    RunError,
    RunSource,
    RunStatus,
    RuntimeAdapter,
    RuntimeMode,
    RuntimeStatus,
    TriggerType,
)
from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    """Reject undeclared fields in Workspace-owned API records."""

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(APIModel):
    """Common FastAPI error body returned for documented HTTP failures."""

    detail: str


class ManifestReloadErrorResponse(APIModel):
    """Validation errors returned when an atomic Manifest reload is rejected."""

    detail: str
    errors: list[dict[str, Any]]


class UserRole(StrEnum):
    """Human Workspace authorization role."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class HealthStatus(StrEnum):
    """Observed health classification exposed to operators."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class SchedulerStatus(StrEnum):
    """Control-plane scheduler loop state."""

    RUNNING = "running"
    ERROR = "error"


class InstanceLockStatus(StrEnum):
    """Workspace instance-lease state."""

    HELD = "held"
    UNAVAILABLE = "unavailable"


class ScheduleOutcome(StrEnum):
    """Persisted result of one schedule cursor dispatch attempt."""

    CREATED = "created"
    DUPLICATE = "duplicate"
    MISFIRE_SKIPPED = "misfire_skipped"
    OVERLAP_SKIPPED = "overlap_skipped"
    QUEUE_FULL = "queue_full"


class OperationStatus(StrEnum):
    """Acknowledgement state for asynchronous control operations."""

    ACCEPTED = "accepted"
    REVOKED = "revoked"


class PluginStatus(StrEnum):
    """Capability-report status for a registered plugin."""

    REPORTED = "reported"


class LogSource(StrEnum):
    """Runtime adapter that supplied a log tail."""

    PROCESS = "process"
    DOCKER = "docker"
    EXTERNAL = "external"


class AuditActorType(StrEnum):
    """Authenticated subject class responsible for an audited action."""

    HUMAN = "human"
    AGENT = "agent"
    WEBHOOK = "webhook"
    SCHEDULER = "scheduler"


class AuditRole(StrEnum):
    """Effective role attached to an audited human or machine subject."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    AGENT = "agent"
    WEBHOOK = "webhook"
    SCHEDULER = "scheduler"


class AuditOutcome(StrEnum):
    """Persisted outcome of an audited control-plane action."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CREATED = "created"
    DUPLICATE = "duplicate"
    MISFIRE_SKIPPED = "misfire_skipped"
    OVERLAP_SKIPPED = "overlap_skipped"
    QUEUE_FULL = "queue_full"


class AuthMeResponse(APIModel):
    """Current authenticated human and CSRF session context."""

    subject: str
    name: str
    roles: list[UserRole]
    csrf_token: str


class AgentCounts(APIModel):
    """Dashboard counts grouped by Agent desired or actual state."""

    total: int
    running: int
    stopped: int
    failed: int


class RunCounts(APIModel):
    """Dashboard counts for active and failed Runs."""

    active: int
    failed: int


class UsageSummary(APIModel):
    """Aggregate provider-reported model usage over a selected period."""

    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: Decimal
    currency: str | None


class UsageView(APIModel):
    """One persisted provider-reported usage record."""

    id: int
    run_id: str | None
    agent_id: str
    provider: str | None
    model: str | None
    request_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    estimated_cost: Decimal | None
    currency: str | None
    recorded_at: datetime


class AnomalyView(APIModel):
    """Recent warning or error event shown on the dashboard."""

    id: str
    severity: EventSeverity
    agent_id: str | None
    run_id: str | None = None
    occurred_at: datetime
    message: str


class ScheduleView(APIModel):
    """Materialized schedule and its persistent dispatch cursor."""

    id: int
    agent_id: str
    trigger_id: str
    handler: str
    cron: str
    timezone: str
    overlap: OverlapPolicy
    misfire_grace_seconds: int
    enabled: bool
    next_fire_at: datetime
    last_fire_at: datetime | None
    last_outcome: ScheduleOutcome | None


class DashboardResponse(APIModel):
    """Workspace dashboard summary assembled from current persisted state."""

    agents: AgentCounts
    runs: RunCounts
    usage_today: UsageSummary
    recent_anomalies: list[AnomalyView]
    next_schedules: list[ScheduleView]


class RunSummary(APIModel):
    """Compact latest-Run record embedded in an Agent summary."""

    run_id: str
    agent_id: str
    handler: str
    source: RunSource
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class AgentSummary(APIModel):
    """One Agent Definition with its current operational summary."""

    agent_id: str
    display_name: str
    description: str | None
    version: str | None
    runtime_adapter: RuntimeAdapter
    runtime_mode: RuntimeMode
    desired_state: DesiredState
    actual_state: RuntimeStatus
    last_heartbeat: datetime | None
    active_runs: int
    last_run: RunSummary | None
    labels: dict[str, str]


class HealthCheckView(APIModel):
    """One named health check reported by a runtime."""

    name: str
    status: Literal[HealthStatus.HEALTHY, HealthStatus.UNHEALTHY]
    detail: str | None


class HealthDetail(APIModel):
    """Agent health classification, heartbeat age, and named checks."""

    status: HealthStatus
    heartbeat_age_seconds: float | None
    checks: list[HealthCheckView]


class PluginView(APIModel):
    """Registered SDK plugin capability."""

    name: str
    version: str | None = None
    critical: bool = False
    status: PluginStatus = PluginStatus.REPORTED
    error: str | None = None


class RuntimeView(APIModel):
    """One concrete managed or externally registered Runtime Instance."""

    runtime_instance_id: str
    agent_id: str
    adapter: RuntimeAdapter
    mode: RuntimeMode
    status: RuntimeStatus
    pid: int | None
    container_id: str | None
    endpoint: str | None
    control_url: str | None
    health_url: str | None
    log_url: str | None
    trace_url: str | None
    restart_attempts: int
    started_at: datetime | None
    ready_at: datetime | None
    stopped_at: datetime | None
    last_heartbeat_at: datetime | None
    last_exit_code: int | None
    last_error: str | None
    metadata: dict[str, Any]


class HandlerView(APIModel):
    """Registered handler schema plus Manifest-owned admission limits."""

    name: str
    description: str | None
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    default_timeout_seconds: int | None
    max_concurrency: int | None
    queue_capacity: int | None
    queue_policy: QueuePolicy | None


class TriggerView(APIModel):
    """Manifest trigger materialized for management inspection."""

    trigger_id: str
    agent_id: str
    type: TriggerType
    handler: str
    enabled: bool
    cron: str | None
    timezone: str | None
    overlap: OverlapPolicy | None


class AgentDetail(AgentSummary):
    """Complete management view of one Agent Definition and current records."""

    manifest: AgentManifest
    descriptor: AgentDescriptor | None
    handlers: list[HandlerView]
    plugins: list[PluginView]
    runtime_instances: list[RuntimeView]
    triggers: list[TriggerView]
    schedules: list[ScheduleView]
    usage: list[UsageView]
    health: HealthDetail
    log_url: str | None
    trace_url: str | None


class RunCreateRequest(APIModel):
    """Operator request to create an on-demand Run."""

    handler: str | None = None
    input: Any = Field(default_factory=dict)
    parent_run_id: str | None = None
    correlation_id: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)


class RunView(APIModel):
    """Persisted Run state, lineage, outcome, usage, and operator links."""

    run_id: str
    agent_id: str
    runtime_instance_id: str | None
    handler: str
    source: RunSource
    trigger_id: str | None
    parent_run_id: str | None
    correlation_id: str
    trace_id: str | None
    status: RunStatus
    input: Any | None
    output: Any | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    deadline: datetime | None
    error: RunError | None
    usage: list[UsageView] = Field(default_factory=list)
    log_url: str | None
    trace_url: str | None
    children: list[str] | None = None


class EventView(APIModel):
    """One deduplicated Event accepted from an Agent."""

    event_id: str
    type: str
    occurred_at: datetime
    received_at: datetime
    agent_id: str
    runtime_instance_id: str | None
    run_id: str | None
    parent_run_id: str | None
    correlation_id: str | None
    trace_id: str | None
    severity: EventSeverity
    payload: dict[str, Any]


class AuditView(APIModel):
    """Immutable audit record for a control-plane action."""

    id: int
    occurred_at: datetime
    actor_type: AuditActorType
    actor_id: str
    actor_role: AuditRole | None
    request_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    remote_address: str | None
    details: dict[str, Any]


class LogsResponse(APIModel):
    """Bounded redacted log tail read from a Runtime Adapter."""

    runtime_instance_id: str
    source: LogSource
    lines: list[str]
    truncated: bool


class RuntimeCounts(APIModel):
    """Workspace health counts for notable Runtime states."""

    ready: int
    unhealthy: int
    lost: int


class WorkspaceHealthResponse(APIModel):
    """Database, lease, scheduler, and Runtime readiness summary."""

    status: HealthStatus
    database: HealthStatus
    instance_lock: InstanceLockStatus
    scheduler: SchedulerStatus
    runtimes: RuntimeCounts
    timestamp: datetime


class OperationResponse(APIModel):
    """Asynchronous lifecycle or credential-revocation acknowledgement."""

    status: OperationStatus
    resource_id: str | None = None


class ManifestReloadResponse(APIModel):
    """Atomic Manifest reload result grouped by definition change kind."""

    loaded: int
    added: list[str]
    updated: list[str]
    removed: list[str]
    unchanged: list[str]
    loaded_at: datetime


class RegistrationResponse(APIModel):
    """Accepted Agent descriptor and current Runtime Instance identity."""

    agent_id: str
    runtime_instance_id: str
    status: RuntimeStatus


class EventBatchResponse(APIModel):
    """Count of inserted Events and already-seen Event IDs."""

    accepted: int
    duplicates: list[str]


class TokenIssueRequest(APIModel):
    """Admin request to issue a revocable Agent bearer credential."""

    agent_id: str
    description: str | None = None
    expires_at: datetime | None = None


class TokenIssueResponse(APIModel):
    """One-time plaintext Agent credential issue response."""

    credential_id: str
    agent_id: str
    token: str
    issued_at: datetime
    expires_at: datetime | None


class TokenView(APIModel):
    """Credential metadata that never exposes the stored token hash."""

    credential_id: str
    agent_id: str
    description: str | None
    issued_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
