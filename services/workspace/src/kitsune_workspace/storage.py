"""Transactional per-Agent retained-storage admission and accounting."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import WorkspaceSettings
from .models import (
    AgentDefinition,
    AgentStorageUsage,
    Run,
    RuntimeInstance,
)
from .util import json_size

RUN_TERMINAL_ERROR_HEADROOM_BYTES = 1_024
RUN_TERMINAL_EVENT_HEADROOM_BYTES = 1_024


class StorageQuotaExceeded(ValueError):
    """Raised when an Agent would exceed a retained-storage limit."""


def lock_agent_definition(session: Session, agent_id: str) -> AgentDefinition | None:
    """Lock one Agent Definition before any storage admission decision."""

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite" and not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))
    query = select(AgentDefinition).where(AgentDefinition.agent_id == agent_id)
    if dialect == "postgresql":
        query = query.with_for_update()
    return session.scalar(query)


def run_json_charge(run: Run) -> int:
    """Return the bytes represented by persisted Run JSON fields."""

    return (
        RUN_TERMINAL_ERROR_HEADROOM_BYTES
        + RUN_TERMINAL_EVENT_HEADROOM_BYTES
        + json_size(run.input)
        + sum(json_size(value) for value in (run.output, run.error) if value is not None)
    )


class StorageQuotaService:
    """Serialize and enforce retained row-count and JSON-byte limits per Agent."""

    def __init__(self, settings: WorkspaceSettings) -> None:
        self.limits = settings.events

    def lock(
        self, session: Session, agent_id: str
    ) -> tuple[AgentDefinition | None, AgentStorageUsage | None]:
        """Lock the Agent Definition, then its quota row in the global lock order."""

        definition = lock_agent_definition(session, agent_id)
        if definition is None:
            return None, None
        return definition, self.lock_usage(session, agent_id)

    def lock_usage(self, session: Session, agent_id: str) -> AgentStorageUsage:
        """Lock the quota row whose presence is an Agent Definition invariant."""

        query = select(AgentStorageUsage).where(AgentStorageUsage.agent_id == agent_id)
        if session.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        usage = session.scalar(query)
        if usage is None:
            raise RuntimeError(f"Agent storage usage row is missing: {agent_id}")
        return usage

    def reserve(
        self,
        usage: AgentStorageUsage,
        *,
        runs: int = 0,
        events: int = 0,
        runtimes: int = 0,
        payload_bytes: int = 0,
    ) -> None:
        """Atomically reserve positive retained counts and JSON bytes on a locked row."""

        if min(runs, events, runtimes, payload_bytes) < 0:
            raise ValueError("Storage reservations cannot be negative")
        if runs == events == runtimes == payload_bytes == 0:
            return
        projected_runs = int(usage.retained_run_count) + runs
        projected_events = int(usage.retained_event_count) + events
        projected_runtimes = int(usage.retained_runtime_count) + runtimes
        projected_payload = int(usage.reserved_payload_bytes) + payload_bytes
        checks = (
            (projected_runs, self.limits.max_runs_per_agent, "Run count"),
            (projected_events, self.limits.max_events_per_agent, "Event count"),
            (
                projected_runtimes,
                self.limits.max_runtime_instances_per_agent,
                "Runtime Instance count",
            ),
            (
                projected_payload,
                self.limits.max_reserved_json_bytes_per_agent,
                "reserved JSON bytes",
            ),
        )
        for projected, maximum, label in checks:
            if projected > maximum:
                raise StorageQuotaExceeded(f"Agent retained {label} quota exceeded")
        usage.retained_run_count = projected_runs
        usage.retained_event_count = projected_events
        usage.retained_runtime_count = projected_runtimes
        usage.reserved_payload_bytes = projected_payload
        usage.lock_version = int(usage.lock_version) + 1

    def ensure_active_runtime_capacity(
        self, session: Session, agent_id: str, *, additional: int = 1
    ) -> None:
        """Reject creation beyond the tight live-runtime cap while the Agent is locked."""

        active = (
            session.scalar(
                select(func.count())
                .select_from(RuntimeInstance)
                .where(
                    RuntimeInstance.agent_id == agent_id,
                    RuntimeInstance.status.in_(
                        ["pending", "starting", "ready", "unhealthy", "stopping"]
                    ),
                )
            )
            or 0
        )
        if active + additional > self.limits.max_active_runtime_instances_per_agent:
            raise StorageQuotaExceeded("Agent active Runtime Instance quota exceeded")

    def ensure_terminal_event_capacity_for_new_run(
        self, session: Session, usage: AgentStorageUsage, agent_id: str
    ) -> None:
        """Keep one retained Event slot available for every nonterminal Run."""

        active_runs = (
            session.scalar(
                select(func.count())
                .select_from(Run)
                .where(
                    Run.agent_id == agent_id,
                    Run.status.not_in(["succeeded", "failed", "cancelled", "timed_out"]),
                )
            )
            or 0
        )
        if int(usage.retained_event_count) + active_runs + 1 > self.limits.max_events_per_agent:
            raise StorageQuotaExceeded("Agent terminal Event capacity is exhausted")

    def reserve_run_growth(
        self, usage: AgentStorageUsage, run: Run, new_charge: int | None = None
    ) -> int:
        """Reserve only positive growth of a Run's conservative JSON charge."""

        current = int(run.storage_charge_bytes or 0)
        target = run_json_charge(run) if new_charge is None else max(0, int(new_charge))
        growth = max(0, target - current)
        if growth:
            self.reserve(usage, payload_bytes=growth)
            run.storage_charge_bytes = target
        return growth

    def reserve_run_field_change(
        self,
        usage: AgentStorageUsage,
        run: Run,
        *,
        previous: Any | None,
        replacement: Any | None,
    ) -> int:
        """Charge positive growth of one Run JSON field without refunding later clears."""

        previous_size = 0 if previous is None else json_size(previous)
        replacement_size = 0 if replacement is None else json_size(replacement)
        growth = max(0, replacement_size - previous_size)
        if growth:
            self.reserve(usage, payload_bytes=growth)
            run.storage_charge_bytes = int(run.storage_charge_bytes or 0) + growth
        return growth

    @staticmethod
    def set_internal_run_error(run: Run, error: dict[str, Any]) -> None:
        """Persist a bounded system error covered by Run-creation headroom."""

        if json_size(error) > RUN_TERMINAL_ERROR_HEADROOM_BYTES:
            raise RuntimeError("Internal Run error exceeds reserved terminal headroom")
        run.error = error

    @staticmethod
    def release(
        usage: AgentStorageUsage,
        *,
        runs: int = 0,
        events: int = 0,
        runtimes: int = 0,
        payload_bytes: int = 0,
    ) -> None:
        """Refund exact deleted-row charges and reject accounting underflow."""

        if min(runs, events, runtimes, payload_bytes) < 0:
            raise ValueError("Storage refunds cannot be negative")
        current = (
            int(usage.retained_run_count),
            int(usage.retained_event_count),
            int(usage.retained_runtime_count),
            int(usage.reserved_payload_bytes),
        )
        refund = (runs, events, runtimes, payload_bytes)
        if any(amount > retained for amount, retained in zip(refund, current, strict=True)):
            raise RuntimeError("Agent storage accounting underflow")
        usage.retained_run_count = current[0] - runs
        usage.retained_event_count = current[1] - events
        usage.retained_runtime_count = current[2] - runtimes
        usage.reserved_payload_bytes = current[3] - payload_bytes
        usage.lock_version = int(usage.lock_version) + 1


def compact_json_charge(value: Any) -> int:
    """Name the canonical compact UTF-8 JSON charge used by storage admission."""

    return json_size(value)
