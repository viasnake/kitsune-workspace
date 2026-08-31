"""Create the complete frozen Workspace control-plane schema.

Revision ID: 0001_workspace_schema
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_workspace_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all initial Workspace tables, constraints, and indexes explicitly."""

    op.create_table(
        "agent_definitions",
        sa.Column("agent_id", sa.String(length=255), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("runtime_adapter", sa.String(length=32), nullable=False),
        sa.Column("runtime_mode", sa.String(length=32), nullable=False),
        sa.Column("desired_state", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_storage_usage",
        sa.Column("agent_id", sa.String(length=255), primary_key=True),
        sa.Column("retained_run_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retained_event_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retained_runtime_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_payload_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lock_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("retained_run_count >= 0", name="ck_agent_storage_runs_nonnegative"),
        sa.CheckConstraint("retained_event_count >= 0", name="ck_agent_storage_events_nonnegative"),
        sa.CheckConstraint(
            "retained_runtime_count >= 0", name="ck_agent_storage_runtimes_nonnegative"
        ),
        sa.CheckConstraint(
            "reserved_payload_bytes >= 0", name="ck_agent_storage_payload_nonnegative"
        ),
        sa.CheckConstraint("lock_version >= 0", name="ck_agent_storage_version_nonnegative"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "audit_records",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("actor_role", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("remote_address", sa.String(length=255), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_occurred", "audit_records", ["occurred_at"])
    op.create_index("ix_audit_records_action", "audit_records", ["action"])
    op.create_index("ix_audit_records_request_id", "audit_records", ["request_id"])
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(length=36), primary_key=True),
        sa.Column("type", sa.String(length=256), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("runtime_instance_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("storage_charge_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "storage_charge_bytes >= 0", name="ck_events_storage_charge_nonnegative"
        ),
    )
    op.create_index("ix_events_agent_id", "events", ["agent_id"])
    op.create_index("ix_events_agent_occurred", "events", ["agent_id", "occurred_at"])
    op.create_index("ix_events_correlation_id", "events", ["correlation_id"])
    op.create_index("ix_events_run_id", "events", ["run_id"])
    op.create_index("ix_events_run_occurred", "events", ["run_id", "occurred_at"])
    op.create_index("ix_events_runtime_instance_id", "events", ["runtime_instance_id"])
    op.create_index("ix_events_trace_id", "events", ["trace_id"])
    op.create_index("ix_events_type", "events", ["type"])
    op.create_table(
        "usage_records",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("request_count", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("storage_charge_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("storage_charge_bytes >= 0", name="ck_usage_storage_charge_nonnegative"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("ix_usage_recorded", "usage_records", ["recorded_at"])
    op.create_index("ix_usage_records_agent_id", "usage_records", ["agent_id"])
    op.create_index("ix_usage_records_run_id", "usage_records", ["run_id"])
    op.create_index("ix_usage_run", "usage_records", ["run_id"])
    op.create_table(
        "workspace_locks",
        sa.Column("lock_name", sa.String(length=255), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_credentials",
        sa.Column("credential_id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('issued', 'manifest_bootstrap')",
            name="ck_agent_credentials_kind",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_agent_credentials_agent_id", "agent_credentials", ["agent_id"])
    op.create_table(
        "agent_descriptors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("application_version", sa.String(length=255), nullable=False),
        sa.Column("sdk_version", sa.String(length=255), nullable=False),
        sa.Column("framework", sa.String(length=255), nullable=True),
        sa.Column("build_revision", sa.String(length=255), nullable=True),
        sa.Column("plugins", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_agent_descriptors_agent_id", "agent_descriptors", ["agent_id"], unique=True)
    op.create_table(
        "handlers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=True),
        sa.Column("output_schema", sa.JSON(), nullable=True),
        sa.Column("default_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("queue_capacity", sa.Integer(), nullable=True),
        sa.Column("queue_policy", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id", "name", name="uq_handlers_agent_name"),
    )
    op.create_index("ix_handlers_agent_id", "handlers", ["agent_id"])
    op.create_table(
        "runtime_instances",
        sa.Column("runtime_instance_id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("container_id", sa.String(length=255), nullable=True),
        sa.Column("control_url", sa.Text(), nullable=True),
        sa.Column("health_url", sa.Text(), nullable=True),
        sa.Column("log_url", sa.Text(), nullable=True),
        sa.Column("trace_url", sa.Text(), nullable=True),
        sa.Column("restart_attempts", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("container_removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_exit_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("runtime_metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_runtime_agent_status", "runtime_instances", ["agent_id", "status"])
    op.create_index("ix_runtime_heartbeat", "runtime_instances", ["last_heartbeat_at"])
    op.create_index("ix_runtime_instances_agent_id", "runtime_instances", ["agent_id"])
    op.create_index("ix_runtime_instances_next_probe_at", "runtime_instances", ["next_probe_at"])
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("trigger_id", sa.String(length=255), nullable=False),
        sa.Column("handler", sa.String(length=255), nullable=False),
        sa.Column("cron", sa.String(length=255), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("overlap", sa.String(length=32), nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id", "trigger_id", name="uq_schedules_agent_id"),
    )
    op.create_index("ix_schedules_agent_id", "schedules", ["agent_id"])
    op.create_index("ix_schedules_next_fire_at", "schedules", ["next_fire_at"])
    op.create_table(
        "triggers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("trigger_id", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("handler", sa.String(length=255), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id", "trigger_id", name="uq_triggers_agent_id"),
    )
    op.create_index("ix_triggers_agent_id", "triggers", ["agent_id"])
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("runtime_instance_id", sa.String(length=36), nullable=True),
        sa.Column("handler", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("trigger_id", sa.String(length=255), nullable=True),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatching_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("log_url", sa.Text(), nullable=True),
        sa.Column("trace_url", sa.Text(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("store_input", sa.Boolean(), nullable=False),
        sa.Column("store_output", sa.Boolean(), nullable=False),
        sa.Column("storage_charge_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("storage_charge_bytes >= 0", name="ck_runs_storage_charge_nonnegative"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["runtime_instance_id"],
            ["runtime_instances.runtime_instance_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["parent_run_id"], ["runs.run_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("agent_id", "idempotency_key", name="uq_runs_agent_idempotency"),
    )
    op.create_index("ix_runs_agent_id", "runs", ["agent_id"])
    op.create_index("ix_runs_agent_status", "runs", ["agent_id", "status"])
    op.create_index("ix_runs_correlation_id", "runs", ["correlation_id"])
    op.create_index("ix_runs_created", "runs", ["created_at"])
    op.create_index("ix_runs_deadline", "runs", ["deadline"])
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"])
    op.create_index("ix_runs_runtime_instance_id", "runs", ["runtime_instance_id"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_trace_id", "runs", ["trace_id"])
    op.create_index("ix_runs_trigger_id", "runs", ["trigger_id"])


def downgrade() -> None:
    """Drop every initial Workspace table in reverse dependency order."""

    op.drop_table("runs")
    op.drop_table("triggers")
    op.drop_table("schedules")
    op.drop_table("runtime_instances")
    op.drop_table("handlers")
    op.drop_table("agent_descriptors")
    op.drop_table("agent_credentials")
    op.drop_table("workspace_locks")
    op.drop_table("usage_records")
    op.drop_table("events")
    op.drop_table("audit_records")
    op.drop_table("agent_storage_usage")
    op.drop_table("agent_definitions")
