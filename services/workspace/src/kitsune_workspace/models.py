"""Portable SQLite and PostgreSQL persistence models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative model base."""


class AgentDefinition(Base):
    """Snapshot of one manifest-backed Agent Definition."""

    __tablename__ = "agent_definitions"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    runtime_adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentStorageUsage(Base):
    """Transactional retained-storage counters for one Agent Definition."""

    __tablename__ = "agent_storage_usage"
    __table_args__ = (
        CheckConstraint("retained_run_count >= 0", name="ck_agent_storage_runs_nonnegative"),
        CheckConstraint("retained_event_count >= 0", name="ck_agent_storage_events_nonnegative"),
        CheckConstraint(
            "retained_runtime_count >= 0", name="ck_agent_storage_runtimes_nonnegative"
        ),
        CheckConstraint("reserved_payload_bytes >= 0", name="ck_agent_storage_payload_nonnegative"),
        CheckConstraint("lock_version >= 0", name="ck_agent_storage_version_nonnegative"),
    )

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), primary_key=True
    )
    retained_run_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    retained_event_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    retained_runtime_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    reserved_payload_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    lock_version: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )


class AgentDescriptor(Base):
    """Latest capability report from an Agent SDK process."""

    __tablename__ = "agent_descriptors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), unique=True, index=True
    )
    application_version: Mapped[str] = mapped_column(String(255), nullable=False)
    sdk_version: Mapped[str] = mapped_column(String(255), nullable=False)
    framework: Mapped[str | None] = mapped_column(String(255))
    build_revision: Mapped[str | None] = mapped_column(String(255))
    plugins: Mapped[list[Any]] = mapped_column(JSON, default=list)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Handler(Base):
    """One named invocation entry point reported by an Agent."""

    __tablename__ = "handlers"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_handlers_agent_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    default_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    max_concurrency: Mapped[int | None] = mapped_column(Integer)
    queue_capacity: Mapped[int | None] = mapped_column(Integer)
    queue_policy: Mapped[str | None] = mapped_column(String(32))


class RuntimeInstance(Base):
    """One concrete process, container, or externally hosted Agent instance."""

    __tablename__ = "runtime_instances"
    __table_args__ = (
        Index("ix_runtime_agent_status", "agent_id", "status"),
        Index("ix_runtime_heartbeat", "last_heartbeat_at"),
    )

    runtime_instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), index=True
    )
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    container_id: Mapped[str | None] = mapped_column(String(255))
    control_url: Mapped[str | None] = mapped_column(Text)
    health_url: Mapped[str | None] = mapped_column(Text)
    log_url: Mapped[str | None] = mapped_column(Text)
    trace_url: Mapped[str | None] = mapped_column(Text)
    restart_attempts: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    probe_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    container_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_exit_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Trigger(Base):
    """Manifest-declared cause capable of creating Runs."""

    __tablename__ = "triggers"
    __table_args__ = (UniqueConstraint("agent_id", "trigger_id", name="uq_triggers_agent_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), index=True
    )
    trigger_id: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    handler: Mapped[str] = mapped_column(String(255), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Schedule(Base):
    """Persistent scheduling cursor for a schedule Trigger."""

    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("agent_id", "trigger_id", name="uq_schedules_agent_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), index=True
    )
    trigger_id: Mapped[str] = mapped_column(String(255), nullable=False)
    handler: Mapped[str] = mapped_column(String(255), nullable=False)
    cron: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    overlap: Mapped[str] = mapped_column(String(32), nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_fire_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(64))


class Run(Base):
    """One requested execution with immutable terminal outcome."""

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_agent_status", "agent_id", "status"),
        Index("ix_runs_created", "created_at"),
        UniqueConstraint("agent_id", "idempotency_key", name="uq_runs_agent_idempotency"),
        CheckConstraint("storage_charge_bytes >= 0", name="ck_runs_storage_charge_nonnegative"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), index=True
    )
    runtime_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime_instances.runtime_instance_id", ondelete="SET NULL"), index=True
    )
    handler: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_id: Mapped[str | None] = mapped_column(String(255), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.run_id", ondelete="SET NULL"), index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    input: Mapped[Any | None] = mapped_column(JSON)
    output: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatching_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    log_url: Mapped[str | None] = mapped_column(Text)
    trace_url: Mapped[str | None] = mapped_column(Text)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    store_input: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    store_output: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    storage_charge_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )


class Event(Base):
    """Deduplicated immutable Kitsune Event."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_run_occurred", "run_id", "occurred_at"),
        Index("ix_events_agent_occurred", "agent_id", "occurred_at"),
        CheckConstraint("storage_charge_bytes >= 0", name="ck_events_storage_charge_nonnegative"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    runtime_instance_id: Mapped[str | None] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    storage_charge_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )


class UsageRecord(Base):
    """Provider-reported usage without inferred missing values."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_run", "run_id"),
        Index("ix_usage_recorded", "recorded_at"),
        CheckConstraint("storage_charge_bytes >= 0", name="ck_usage_storage_charge_nonnegative"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    event_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    provider: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    request_count: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    currency: Mapped[str | None] = mapped_column(String(16))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    storage_charge_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )


class AuditRecord(Base):
    """Operator and agent control-plane audit entry."""

    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_occurred", "occurred_at"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_address: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentCredential(Base):
    """Hashed Agent-specific bearer credential."""

    __tablename__ = "agent_credentials"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('issued', 'manifest_bootstrap')",
            name="ck_agent_credentials_kind",
        ),
    )

    credential_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceLock(Base):
    """Lease used to enforce the single-control-plane deployment model."""

    __tablename__ = "workspace_locks"

    lock_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
