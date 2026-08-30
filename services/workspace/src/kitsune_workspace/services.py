"""Run, event, usage, audit, queue, timeout, and retention services."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kitsune.logging import is_sensitive_key, normalize_redacted_keys, redact_sensitive_data
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import WorkspaceSettings, resolve_secret_reference
from .database import Database
from .models import (
    AgentDefinition,
    AgentStorageUsage,
    AuditRecord,
    Event,
    Handler,
    Run,
    RuntimeInstance,
    Schedule,
    UsageRecord,
)
from .runtime import RuntimeDispatchUncertain, RuntimeManager, RuntimeOperationError
from .storage import (
    RUN_TERMINAL_ERROR_HEADROOM_BYTES,
    RUN_TERMINAL_EVENT_HEADROOM_BYTES,
    StorageQuotaExceeded,
    StorageQuotaService,
    compact_json_charge,
    lock_agent_definition,
)
from .util import (
    ACTIVE_RUN_STATUSES,
    PENDING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ensure_aware,
    json_size,
    nested,
    redact,
    redact_values,
    utcnow,
)

if TYPE_CHECKING:
    from .telemetry import WorkspaceTelemetry


class RunServiceError(ValueError):
    """Base class for actionable Run request errors."""


class QueueCapacityExceeded(RunServiceError):
    """Raised when queue policy rejects or capacity is exhausted."""


class InvalidRunTransition(RunServiceError):
    """Raised when a terminal or otherwise invalid state change is attempted."""


def audit(
    session: Session,
    *,
    actor_type: str,
    actor_id: str,
    actor_role: str | None = None,
    request_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str = "succeeded",
    remote_address: str | None = None,
    details: dict[str, Any] | None = None,
    redacted_keys: set[str] | None = None,
) -> AuditRecord:
    """Append an immutable audit record."""

    record = AuditRecord(
        occurred_at=utcnow(),
        actor_type=actor_type,
        actor_id=actor_id,
        actor_role=actor_role,
        request_id=request_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        remote_address=remote_address,
        details=redact(details or {}, redacted_keys or set()),
    )
    session.add(record)
    return record


def _transition(run: Run, target: str, at: datetime | None = None) -> None:
    if target == run.status:
        return
    from kitsune_contracts import RunStatus, validate_run_transition

    try:
        validate_run_transition(RunStatus(run.status), RunStatus(target))
    except ValueError as exc:
        raise InvalidRunTransition(f"Run cannot transition from {run.status} to {target}") from exc
    now = at or utcnow()
    run.status = target
    if target == "queued":
        run.queued_at = run.queued_at or now
    elif target == "dispatching":
        run.dispatching_at = now
    elif target == "running":
        run.started_at = run.started_at or now
    elif target in TERMINAL_RUN_STATUSES:
        run.ended_at = run.ended_at or now
        if not run.store_input:
            run.input = None
        if not run.store_output:
            run.output = None


def _invocation(snapshot: dict[str, Any]) -> dict[str, Any]:
    return nested(snapshot, "spec", "invocation", default={}) or {}


def _handler_invocation(invocation: dict[str, Any], handler: str) -> dict[str, Any]:
    handlers = invocation.get("handlers") or {}
    if not isinstance(handlers, dict):
        return {}
    selected = handlers.get(handler) or {}
    return selected if isinstance(selected, dict) else {}


def _normalise_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_aware(parsed)


_INPUT_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "args",
        "arguments",
        "input",
        "inputdata",
        "inputs",
        "inputvalue",
        "message",
        "messages",
        "prompt",
        "prompts",
        "query",
        "request",
        "requests",
        "toolarguments",
        "toolargs",
        "toolinput",
        "toolinputs",
    }
)
_OUTPUT_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "chunk",
        "completion",
        "completions",
        "content",
        "message",
        "messages",
        "output",
        "outputdata",
        "outputs",
        "outputvalue",
        "response",
        "responses",
        "result",
        "results",
        "toolcall",
        "toolcalls",
        "tooloutput",
        "tooloutputs",
        "toolresult",
        "toolresults",
    }
)
_STRUCTURAL_STRING_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "code",
        "currency",
        "dimension",
        "error_type",
        "event",
        "handler",
        "hook",
        "model",
        "name",
        "operation",
        "phase",
        "plugin",
        "provider",
        "reason",
        "rejected_event_id",
        "rejected_event_type",
        "run_id",
        "scope",
        "source",
        "state",
        "status",
        "type",
        "version",
    }
)
_STRUCTURAL_NUMBER_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "cache_read_tokens",
        "cache_write_tokens",
        "child_runs",
        "completed",
        "estimated_cost",
        "events_observed",
        "index",
        "input_tokens",
        "iteration",
        "limit",
        "model_requests",
        "output_tokens",
        "request_count",
        "size_bytes",
        "status_code",
        "step",
        "total",
        "total_tokens",
        "used",
        "wall_clock_seconds",
    }
)
_STRUCTURAL_BOOLEAN_EVENT_PAYLOAD_FIELDS = frozenset({"finalization", "retryable"})
_STRUCTURAL_STRING_LIST_EVENT_PAYLOAD_FIELDS = frozenset({"parent_ids", "soft_dimensions"})
_STRUCTURAL_OBJECT_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "error",
        "input_omitted",
        "output_omitted",
        "output_rejected",
        "payload_rejected",
        "usage",
    }
)
_STRUCTURAL_STRING_LIMIT = 256
_STRUCTURAL_NUMBER_STRING_LIMIT = 64
_STRUCTURAL_LIST_LIMIT = 64


def _normalise_payload_field(value: object) -> str:
    """Normalize JSON field spelling without conflating token counters with content."""

    return "".join(character.casefold() for character in str(value) if character.isalnum())


_NORMALIZED_STRUCTURAL_STRING_EVENT_PAYLOAD_FIELDS = frozenset(
    _normalise_payload_field(item) for item in _STRUCTURAL_STRING_EVENT_PAYLOAD_FIELDS
)
_NORMALIZED_STRUCTURAL_NUMBER_EVENT_PAYLOAD_FIELDS = frozenset(
    _normalise_payload_field(item) for item in _STRUCTURAL_NUMBER_EVENT_PAYLOAD_FIELDS
)
_NORMALIZED_STRUCTURAL_BOOLEAN_EVENT_PAYLOAD_FIELDS = frozenset(
    _normalise_payload_field(item) for item in _STRUCTURAL_BOOLEAN_EVENT_PAYLOAD_FIELDS
)
_NORMALIZED_STRUCTURAL_STRING_LIST_EVENT_PAYLOAD_FIELDS = frozenset(
    _normalise_payload_field(item) for item in _STRUCTURAL_STRING_LIST_EVENT_PAYLOAD_FIELDS
)
_NORMALIZED_STRUCTURAL_OBJECT_EVENT_PAYLOAD_FIELDS = frozenset(
    _normalise_payload_field(item) for item in _STRUCTURAL_OBJECT_EVENT_PAYLOAD_FIELDS
)


def _sanitize_enabled_run_content(
    value: Any,
    *,
    omit_input: bool,
    omit_output: bool,
) -> tuple[Any, bool, bool]:
    """Keep one enabled content subtree while removing a disabled nested category."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        removed_input = False
        removed_output = False
        for key, item in value.items():
            normalized = _normalise_payload_field(key)
            is_input = normalized in _INPUT_EVENT_PAYLOAD_FIELDS
            is_output = normalized in _OUTPUT_EVENT_PAYLOAD_FIELDS
            if (omit_input and is_input) or (omit_output and is_output):
                removed_input = removed_input or (omit_input and is_input)
                removed_output = removed_output or (omit_output and is_output)
                continue
            sanitized, nested_input, nested_output = _sanitize_enabled_run_content(
                item,
                omit_input=omit_input,
                omit_output=omit_output,
            )
            result[str(key)] = sanitized
            removed_input = removed_input or nested_input
            removed_output = removed_output or nested_output
        return result, removed_input, removed_output
    if isinstance(value, list):
        items: list[Any] = []
        removed_input = False
        removed_output = False
        for item in value:
            sanitized, nested_input, nested_output = _sanitize_enabled_run_content(
                item,
                omit_input=omit_input,
                omit_output=omit_output,
            )
            items.append(sanitized)
            removed_input = removed_input or nested_input
            removed_output = removed_output or nested_output
        return items, removed_input, removed_output
    return value, False, False


def _sanitize_structural_value(
    normalized: str,
    value: Any,
    *,
    omit_input: bool,
    omit_output: bool,
) -> tuple[bool, Any, bool, bool]:
    """Validate one explicitly safe structural or Usage payload field."""

    if normalized in _NORMALIZED_STRUCTURAL_STRING_EVENT_PAYLOAD_FIELDS:
        accepted = isinstance(value, str) and len(value) <= _STRUCTURAL_STRING_LIMIT
        return accepted, value, False, False
    if normalized in _NORMALIZED_STRUCTURAL_NUMBER_EVENT_PAYLOAD_FIELDS:
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        is_number_string = isinstance(value, str) and len(value) <= _STRUCTURAL_NUMBER_STRING_LIMIT
        accepted = False
        if is_number or is_number_string:
            with contextlib.suppress(ValueError, OverflowError):
                accepted = math.isfinite(float(value))
        return accepted, value, False, False
    if normalized in _NORMALIZED_STRUCTURAL_BOOLEAN_EVENT_PAYLOAD_FIELDS:
        return isinstance(value, bool), value, False, False
    if normalized in _NORMALIZED_STRUCTURAL_STRING_LIST_EVENT_PAYLOAD_FIELDS:
        accepted = (
            isinstance(value, list)
            and len(value) <= _STRUCTURAL_LIST_LIMIT
            and all(
                isinstance(item, str) and len(item) <= _STRUCTURAL_STRING_LIMIT for item in value
            )
        )
        return accepted, value, False, False
    if normalized in _NORMALIZED_STRUCTURAL_OBJECT_EVENT_PAYLOAD_FIELDS:
        if not isinstance(value, dict):
            return False, None, False, False
        sanitized, removed_input, removed_output = _sanitize_restricted_payload_mapping(
            value,
            omit_input=omit_input,
            omit_output=omit_output,
            allow_content=False,
        )
        return True, sanitized, removed_input, removed_output
    return False, None, False, False


def _sanitize_restricted_payload_mapping(
    value: dict[str, Any],
    *,
    omit_input: bool,
    omit_output: bool,
    allow_content: bool,
) -> tuple[dict[str, Any], bool, bool]:
    """Keep only bounded structural fields and explicitly enabled content categories."""

    result: dict[str, Any] = {}
    removed_input = False
    removed_output = False
    for key, item in value.items():
        normalized = _normalise_payload_field(key)
        accepted, sanitized, nested_input, nested_output = _sanitize_structural_value(
            normalized,
            item,
            omit_input=omit_input,
            omit_output=omit_output,
        )
        if accepted:
            result[str(key)] = sanitized
            removed_input = removed_input or nested_input
            removed_output = removed_output or nested_output
            continue
        is_input = normalized in _INPUT_EVENT_PAYLOAD_FIELDS
        is_output = normalized in _OUTPUT_EVENT_PAYLOAD_FIELDS
        blocked_input = omit_input and is_input
        blocked_output = omit_output and is_output
        if allow_content and (is_input or is_output) and not (blocked_input or blocked_output):
            sanitized, nested_input, nested_output = _sanitize_enabled_run_content(
                item,
                omit_input=omit_input,
                omit_output=omit_output,
            )
            result[str(key)] = sanitized
            removed_input = removed_input or nested_input
            removed_output = removed_output or nested_output
            continue
        removed_input = removed_input or blocked_input or (omit_input and not is_output)
        removed_output = removed_output or blocked_output or (omit_output and not is_input)
    return result, removed_input, removed_output


def _apply_run_event_persistence_policy(payload: dict[str, Any], run: Run) -> dict[str, Any]:
    """Apply one Run's input and output persistence policy to every Event payload."""

    if run.store_input and run.store_output:
        return payload
    sanitized, omitted_input, omitted_output = _sanitize_restricted_payload_mapping(
        payload,
        omit_input=not run.store_input,
        omit_output=not run.store_output,
        allow_content=True,
    )
    if omitted_input:
        sanitized["input_omitted"] = {"reason": "persistence_disabled"}
    if omitted_output:
        sanitized["output_omitted"] = {"reason": "persistence_disabled"}
    return sanitized


def _resolved_persistence_secrets(
    settings: WorkspaceSettings,
    snapshot: dict[str, Any],
    additional_values: set[str] | None = None,
) -> set[str]:
    """Resolve configured secrets that must never cross a durable JSON boundary."""

    references: list[str] = []
    agent_token = nested(snapshot, "spec", "security", "agent_token_ref")
    if isinstance(agent_token, str):
        references.append(agent_token)
    runtime = nested(snapshot, "spec", "runtime", default={}) or {}
    references.extend(
        reference
        for reference in (runtime.get("secrets") or {}).values()
        if isinstance(reference, str)
    )
    normalized = normalize_redacted_keys(frozenset(settings.security.redacted_keys))
    references.extend(
        reference
        for key, reference in (runtime.get("environment") or {}).items()
        if isinstance(reference, str) and is_sensitive_key(key, normalized)
    )
    for trigger in nested(snapshot, "spec", "triggers", default=[]) or []:
        if not isinstance(trigger, dict):
            continue
        for key in ("shared_secret_ref", "hmac_secret_ref"):
            reference = trigger.get(key)
            if isinstance(reference, str):
                references.append(reference)
    values = set(additional_values or set())
    for configured in (settings.auth.client_secret, settings.auth.session_secret):
        if configured is not None:
            values.add(configured.get_secret_value())
    for reference in references:
        with contextlib.suppress(OSError, ValueError):
            values.add(resolve_secret_reference(reference))
    return {value for value in values if value}


def _idempotency_digest(agent_id: str, key: str) -> str:
    """Return a domain-separated stable digest without retaining the presented key."""

    digest = hashlib.sha256()
    digest.update(b"kitsune.run.idempotency.v1\0")
    digest.update(agent_id.encode())
    digest.update(b"\0")
    digest.update(key.encode())
    return digest.hexdigest()


class RunService:
    """Create, dispatch, cancel, and time-bound persisted Runs."""

    def __init__(
        self,
        database: Database,
        settings: WorkspaceSettings,
        runtime: RuntimeManager,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self.storage = StorageQuotaService(settings)

    def create(
        self,
        *,
        agent_id: str,
        handler: str | None,
        source: str,
        trigger_id: str | None = None,
        input_value: Any,
        parent_run_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
        runtime_instance_id: str | None = None,
        start_running: bool = False,
        require_runtime_assignment: bool = False,
        redacted_values: set[str] | None = None,
    ) -> tuple[Run, bool]:
        """Create a queued Run after enforcing input, lineage, concurrency, and queue policy."""

        if json_size(input_value) > self.settings.events.max_input_bytes:
            raise RunServiceError("Run input exceeds the configured size limit")
        if source == "child" and parent_run_id is None:
            raise RunServiceError("child Run requires parent_run_id")
        if source != "child" and parent_run_id is not None:
            raise RunServiceError("parent_run_id is only valid for a child Run")
        with self.database.session() as session:
            definition = lock_agent_definition(session, agent_id)
            if definition is None or not definition.active:
                raise RunServiceError(f"unknown Agent Definition: {agent_id}")
            storage_usage = self.storage.lock_usage(session, agent_id)
            if definition.desired_state != "running":
                raise RunServiceError(f"Agent {agent_id} desired_state is stopped")
            invocation = _invocation(definition.snapshot)
            persistence_secrets = _resolved_persistence_secrets(
                self.settings,
                definition.snapshot,
                redacted_values,
            )
            self.runtime.telemetry.register_secrets(*persistence_secrets)
            persisted_input = redact_sensitive_data(
                input_value,
                frozenset(self.settings.security.redacted_keys),
                frozenset(persistence_secrets),
            )
            if json_size(persisted_input) > self.settings.events.max_input_bytes:
                raise RunServiceError("Redacted Run input exceeds the configured size limit")
            selected_handler = handler or invocation.get("default_handler")
            if not selected_handler:
                raise RunServiceError(
                    "handler is required because no default_handler is configured"
                )
            known_handlers = set(
                session.scalars(select(Handler.name).where(Handler.agent_id == agent_id)).all()
            )
            manifest_handlers = {
                trigger.get("handler")
                for trigger in nested(definition.snapshot, "spec", "triggers", default=[]) or []
            }
            if (
                known_handlers
                and source != "child"
                and selected_handler not in known_handlers
                and selected_handler not in manifest_handlers
            ):
                raise RunServiceError(f"unknown handler {selected_handler!r} for Agent {agent_id}")
            requested_instance: RuntimeInstance | None = None
            if runtime_instance_id is not None:
                requested_instance = session.get(RuntimeInstance, runtime_instance_id)
                if requested_instance is None or requested_instance.agent_id != agent_id:
                    raise RunServiceError(
                        "requested Runtime Instance is not in scope for the Agent"
                    )
                if requested_instance.status != "ready":
                    raise RunServiceError("requested Runtime Instance is not ready")
            persisted_idempotency_key = (
                _idempotency_digest(agent_id, idempotency_key) if idempotency_key else None
            )
            if persisted_idempotency_key:
                existing = session.scalar(
                    select(Run).where(
                        Run.agent_id == agent_id,
                        Run.idempotency_key == persisted_idempotency_key,
                    )
                )
                if existing is not None:
                    return existing, False
            parent: Run | None = None
            if parent_run_id:
                parent = session.get(Run, parent_run_id)
                if parent is None:
                    raise RunServiceError(f"unknown parent Run: {parent_run_id}")
                if correlation_id and correlation_id != parent.correlation_id:
                    raise RunServiceError("child Run correlation_id must match its parent")
                correlation_id = parent.correlation_id

            self.storage.ensure_terminal_event_capacity_for_new_run(
                session, storage_usage, agent_id
            )

            max_concurrency = int(invocation.get("max_concurrency", 1))
            handler_override = _handler_invocation(invocation, selected_handler)
            handler_record = session.scalar(
                select(Handler).where(
                    Handler.agent_id == agent_id, Handler.name == selected_handler
                )
            )
            handler_limit = int(
                handler_override.get("max_concurrency")
                or (
                    handler_record.max_concurrency
                    if handler_record and handler_record.max_concurrency is not None
                    else max_concurrency
                )
            )
            active_agent = (
                session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(Run.agent_id == agent_id, Run.status.in_(ACTIVE_RUN_STATUSES))
                )
                or 0
            )
            active_handler = (
                session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.agent_id == agent_id,
                        Run.handler == selected_handler,
                        Run.status.in_(ACTIVE_RUN_STATUSES),
                    )
                )
                or 0
            )
            pending_agent = (
                session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.agent_id == agent_id,
                        Run.status.in_(PENDING_RUN_STATUSES),
                    )
                )
                or 0
            )
            pending_handler = (
                session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.agent_id == agent_id,
                        Run.handler == selected_handler,
                        Run.status.in_(PENDING_RUN_STATUSES),
                    )
                )
                or 0
            )
            if start_running:
                if active_agent >= max_concurrency:
                    raise QueueCapacityExceeded("Agent concurrency is exhausted")
                if active_handler >= handler_limit:
                    raise QueueCapacityExceeded(
                        f"Handler {selected_handler!r} concurrency is exhausted"
                    )
            else:
                agent_at_capacity = active_agent + pending_agent >= max_concurrency
                handler_at_capacity = active_handler + pending_handler >= handler_limit
                agent_queue_policy = str(invocation.get("queue_policy", "queue"))
                handler_queue_policy = str(
                    handler_override.get("queue_policy")
                    or (handler_record.queue_policy if handler_record else None)
                    or agent_queue_policy
                )
                if agent_queue_policy == "reject" and agent_at_capacity:
                    raise QueueCapacityExceeded("Agent concurrency is exhausted")
                if handler_queue_policy == "reject" and handler_at_capacity:
                    raise QueueCapacityExceeded(
                        f"Handler {selected_handler!r} concurrency is exhausted"
                    )
                agent_queue_capacity = int(invocation.get("queue_capacity", 0))
                configured_handler_capacity = handler_override.get("queue_capacity")
                if configured_handler_capacity is None:
                    configured_handler_capacity = (
                        handler_record.queue_capacity
                        if handler_record and handler_record.queue_capacity is not None
                        else agent_queue_capacity
                    )
                handler_queue_capacity = int(configured_handler_capacity)
                agent_full = active_agent + pending_agent >= max_concurrency + agent_queue_capacity
                handler_full = (
                    active_handler + pending_handler >= handler_limit + handler_queue_capacity
                )
                if agent_queue_policy == "queue" and agent_full:
                    raise QueueCapacityExceeded("Agent Run queue capacity is exhausted")
                if handler_queue_policy == "queue" and handler_full:
                    raise QueueCapacityExceeded(
                        f"Handler {selected_handler!r} Run queue capacity is exhausted"
                    )

            now = utcnow()
            configured_timeout = int(invocation.get("timeout_seconds", 900))
            effective_timeout = timeout_seconds or (
                handler_record.default_timeout_seconds
                if handler_record and handler_record.default_timeout_seconds
                else configured_timeout
            )
            if effective_timeout > self.settings.events.max_run_timeout_seconds:
                raise RunServiceError("Run timeout exceeds the configured maximum")
            deadline = now + timedelta(seconds=effective_timeout)
            if parent and parent.deadline and deadline > ensure_aware(parent.deadline):
                deadline = ensure_aware(parent.deadline)
            record = Run(
                run_id=run_id or str(uuid.uuid4()),
                agent_id=agent_id,
                handler=selected_handler,
                source=source,
                trigger_id=trigger_id,
                parent_run_id=parent_run_id,
                correlation_id=correlation_id or str(uuid.uuid4()),
                trace_id=trace_id,
                status="created",
                input=persisted_input,
                created_at=now,
                deadline=deadline,
                idempotency_key=persisted_idempotency_key,
                retention_days=int(
                    invocation.get("retention_days", self.settings.events.retention_days)
                ),
                store_input=bool(invocation.get("store_input", True)),
                store_output=bool(invocation.get("store_output", True)),
                storage_charge_bytes=(
                    compact_json_charge(persisted_input)
                    + RUN_TERMINAL_ERROR_HEADROOM_BYTES
                    + RUN_TERMINAL_EVENT_HEADROOM_BYTES
                ),
            )
            if start_running:
                assigned = requested_instance
                if assigned is None and not require_runtime_assignment:
                    assigned = session.scalar(
                        select(RuntimeInstance)
                        .where(
                            RuntimeInstance.agent_id == agent_id,
                            RuntimeInstance.status == "ready",
                        )
                        .order_by(RuntimeInstance.ready_at.desc())
                    )
                if assigned is None and require_runtime_assignment:
                    raise RunServiceError(
                        "Agent-initiated Run requires its ready Runtime Instance assignment"
                    )
                if assigned is not None:
                    record.runtime_instance_id = assigned.runtime_instance_id
            record.log_url, record.trace_url = self.runtime._operator_urls(
                definition.snapshot,
                agent_id=agent_id,
                runtime_instance_id=record.runtime_instance_id or "",
                run=record,
            )
            self.storage.reserve(
                storage_usage,
                runs=1,
                payload_bytes=record.storage_charge_bytes,
            )
            session.add(record)
            _transition(record, "queued", now)
            if start_running:
                _transition(record, "dispatching", now)
                _transition(record, "running", now)
            try:
                session.flush()
            except IntegrityError as exc:
                if persisted_idempotency_key:
                    session.rollback()
                    existing = session.scalar(
                        select(Run).where(
                            Run.agent_id == agent_id,
                            Run.idempotency_key == persisted_idempotency_key,
                        )
                    )
                    if existing is not None:
                        return existing, False
                raise RunServiceError("Run ID or idempotency key already exists") from exc
            created_record = record
        self.runtime.telemetry.runs_total.add(1, {"agent_id": agent_id, "source": source})
        return created_record, True

    async def dispatch_available(self) -> int:
        """Dispatch FIFO queued Runs while Agent and Handler slots remain available."""

        dispatched = await self._retry_resident_dispatches()
        with self.database.session() as session:
            agent_ids = list(
                session.scalars(
                    select(Run.agent_id)
                    .where(Run.status == "queued")
                    .distinct()
                    .order_by(Run.agent_id)
                )
            )
        for agent_id in agent_ids:
            while True:
                with self.database.session() as session:
                    definition = session.get(AgentDefinition, agent_id)
                    if definition is None or not definition.active:
                        break
                    if definition.desired_state != "running":
                        break
                    invocation = _invocation(definition.snapshot)
                    global_limit = int(invocation.get("max_concurrency", 1))
                    active_count = (
                        session.scalar(
                            select(func.count())
                            .select_from(Run)
                            .where(Run.agent_id == agent_id, Run.status.in_(ACTIVE_RUN_STATUSES))
                        )
                        or 0
                    )
                    if active_count >= global_limit:
                        break
                    candidates = list(
                        session.scalars(
                            select(Run)
                            .where(Run.agent_id == agent_id, Run.status == "queued")
                            .order_by(Run.queued_at, Run.created_at)
                            .limit(25)
                        )
                    )
                    selected: Run | None = None
                    for candidate in candidates:
                        if candidate.source == "schedule" and candidate.trigger_id:
                            overlap = session.scalar(
                                select(Schedule.overlap).where(
                                    Schedule.agent_id == agent_id,
                                    Schedule.trigger_id == candidate.trigger_id,
                                )
                            )
                            if overlap == "queue":
                                overlapping = session.scalar(
                                    select(func.count())
                                    .select_from(Run)
                                    .where(
                                        Run.agent_id == agent_id,
                                        Run.trigger_id == candidate.trigger_id,
                                        Run.status.in_(ACTIVE_RUN_STATUSES),
                                    )
                                )
                                if overlapping:
                                    continue
                        handler_override = _handler_invocation(invocation, candidate.handler)
                        handler_record = session.scalar(
                            select(Handler).where(
                                Handler.agent_id == agent_id, Handler.name == candidate.handler
                            )
                        )
                        handler_limit = int(
                            handler_override.get("max_concurrency")
                            or (
                                handler_record.max_concurrency
                                if handler_record and handler_record.max_concurrency is not None
                                else global_limit
                            )
                        )
                        handler_active = (
                            session.scalar(
                                select(func.count())
                                .select_from(Run)
                                .where(
                                    Run.agent_id == agent_id,
                                    Run.handler == candidate.handler,
                                    Run.status.in_(ACTIVE_RUN_STATUSES),
                                )
                            )
                            or 0
                        )
                        if handler_active < handler_limit:
                            selected = candidate
                            break
                    if selected is None:
                        break
                    run_id = selected.run_id
                    mode = definition.runtime_mode
                    definition_snapshot = definition.snapshot
                    instance: RuntimeInstance | None = None
                    if mode != "ephemeral":
                        instance = session.scalar(
                            select(RuntimeInstance)
                            .where(
                                RuntimeInstance.agent_id == agent_id,
                                RuntimeInstance.status == "ready",
                            )
                            .order_by(RuntimeInstance.ready_at.desc())
                        )
                        if instance is None:
                            needs_runtime = True
                        else:
                            needs_runtime = False
                            _transition(selected, "dispatching")
                            selected.runtime_instance_id = instance.runtime_instance_id
                            selected.log_url, selected.trace_url = self.runtime._operator_urls(
                                definition_snapshot,
                                agent_id=agent_id,
                                runtime_instance_id=instance.runtime_instance_id,
                                run=selected,
                            )
                    else:
                        needs_runtime = False
                        _transition(selected, "dispatching")
                if needs_runtime:
                    try:
                        await self.runtime.start_agent(agent_id)
                    except (RuntimeOperationError, StorageQuotaExceeded):
                        pass
                    break
                try:
                    with self.database.session() as session:
                        run = session.get(Run, run_id)
                        if run is None:
                            break
                        detached = run
                    if mode == "ephemeral":
                        await self.runtime.start_agent(agent_id, detached)
                    else:
                        if instance is None:
                            break
                        await self.runtime.publish(
                            "run", {"run_id": run_id, "status": "dispatching"}
                        )
                        await self._submit_resident_run(run_id, instance.runtime_instance_id)
                    dispatched += 1
                    with self.database.session() as session:
                        current = session.get(Run, run_id)
                        published_status = current.status if current else "dispatching"
                    await self.runtime.publish(
                        "run", {"run_id": run_id, "status": published_status}
                    )
                except RuntimeDispatchUncertain:
                    # The Agent may have started the Run before the response was lost. Keep
                    # the durable assignment and retry the idempotent submission next poll.
                    break
                except (RuntimeOperationError, StorageQuotaExceeded) as exc:
                    self._fail_dispatch(run_id, agent_id)
                    await self.runtime.publish("run", {"run_id": run_id, "status": "failed"})
                    if isinstance(exc, StorageQuotaExceeded):
                        break
        return dispatched

    async def _retry_resident_dispatches(self) -> int:
        """Retry resident submissions whose acceptance was not durably observed."""

        with self.database.session() as session:
            pending = list(
                session.execute(
                    select(Run.run_id, Run.agent_id, Run.runtime_instance_id)
                    .join(
                        RuntimeInstance,
                        RuntimeInstance.runtime_instance_id == Run.runtime_instance_id,
                    )
                    .where(
                        Run.status == "dispatching",
                        RuntimeInstance.status == "ready",
                        RuntimeInstance.mode == "resident",
                    )
                    .order_by(Run.dispatching_at, Run.created_at)
                )
            )
        accepted = 0
        for run_id, agent_id, instance_id in pending:
            if not isinstance(instance_id, str):
                continue
            try:
                await self._submit_resident_run(run_id, instance_id)
                accepted += 1
                with self.database.session() as session:
                    current = session.get(Run, run_id)
                    status = current.status if current else "dispatching"
                await self.runtime.publish("run", {"run_id": run_id, "status": status})
            except RuntimeDispatchUncertain:
                continue
            except RuntimeOperationError:
                self._fail_dispatch(run_id, agent_id)
                await self.runtime.publish("run", {"run_id": run_id, "status": "failed"})
        return accepted

    async def _submit_resident_run(self, run_id: str, instance_id: str) -> None:
        """Idempotently submit one durably assigned resident Run and record acceptance."""

        with self.database.session() as session:
            run = session.get(Run, run_id)
            instance = session.get(RuntimeInstance, instance_id)
            if run is None or instance is None:
                raise RuntimeOperationError("resident dispatch assignment disappeared")
            if run.status != "dispatching":
                return
            detached_run = run
            detached_instance = instance
        await self.runtime.dispatch_resident(detached_run, detached_instance)
        with self.database.session() as session:
            accepted = session.get(Run, run_id)
            if accepted is not None and accepted.status == "dispatching":
                _transition(accepted, "running")

    def _fail_dispatch(self, run_id: str, agent_id: str) -> None:
        """Persist one proven resident or ephemeral dispatch rejection."""

        with self.database.session() as session:
            definition = lock_agent_definition(session, agent_id)
            if definition is None:
                return
            failed = session.get(Run, run_id)
            if failed and failed.status not in TERMINAL_RUN_STATUSES:
                _transition(failed, "failed")
                error = {
                    "type": "dispatch_failed",
                    "message": "Runtime dispatch failed",
                    "retryable": False,
                    "details": {},
                }
                self.storage.set_internal_run_error(failed, error)

    async def cancel(self, run_id: str, timed_out: bool = False) -> Run:
        """Cancel a non-terminal Run through its assigned Runtime Adapter."""

        with self.database.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunServiceError(f"unknown Run: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise InvalidRunTransition(f"Run is already terminal: {run.status}")
            run.cancel_requested_at = utcnow()
            detached = run
        await self.runtime.cancel_run(detached, timed_out=timed_out)
        with self.database.session() as session:
            result = session.get(Run, run_id)
            if result is None:
                raise RunServiceError("Run disappeared during cancellation")
            return result

    async def enforce_timeouts(self) -> int:
        """Cancel every non-terminal Run whose persisted Deadline has expired."""

        now = utcnow()
        with self.database.session() as session:
            ids = list(
                session.scalars(
                    select(Run.run_id).where(
                        Run.status.not_in(TERMINAL_RUN_STATUSES),
                        Run.deadline.is_not(None),
                        Run.deadline <= now,
                    )
                )
            )
        count = 0
        for run_id in ids:
            try:
                await self.cancel(run_id, timed_out=True)
                count += 1
            except (RunServiceError, RuntimeOperationError):
                with self.database.session() as session:
                    run = session.get(Run, run_id)
                    if run and run.status not in TERMINAL_RUN_STATUSES:
                        _transition(run, "timed_out")
                count += 1
        return count


class EventService:
    """Validate, deduplicate, persist, and apply Agent event batches."""

    def __init__(
        self,
        database: Database,
        settings: WorkspaceSettings,
        telemetry: WorkspaceTelemetry,
    ) -> None:
        self.database = database
        self.settings = settings
        self.telemetry = telemetry
        self.storage = StorageQuotaService(settings)

    @staticmethod
    def validate_event(raw: dict[str, Any]) -> dict[str, Any]:
        """Validate against the shared contract and return canonical JSON data."""

        from kitsune_contracts import KitsuneEvent

        event = KitsuneEvent.model_validate(raw)
        return event.model_dump(mode="json", by_alias=True, exclude_none=True)

    def ingest(
        self,
        agent_id: str,
        raw_events: list[dict[str, Any]],
        *,
        redacted_values: set[str] | None = None,
    ) -> dict[str, Any]:
        """Apply one all-or-nothing batch with bounded terminal-state fallback."""

        try:
            return self._ingest_once(
                agent_id,
                raw_events,
                redacted_values=redacted_values,
                terminal_quota_fallback=False,
            )
        except StorageQuotaExceeded:
            terminal_types = {
                "kitsune.run.succeeded",
                "kitsune.run.completed",
                "kitsune.run.failed",
                "kitsune.run.cancelled",
                "kitsune.run.timed_out",
            }
            if not raw_events or any(item.get("type") not in terminal_types for item in raw_events):
                raise
            fallback_events = [
                {
                    **item,
                    "payload": {"persistence_rejected": {"reason": "agent_storage_quota"}},
                }
                for item in raw_events
            ]
            return self._ingest_once(
                agent_id,
                fallback_events,
                redacted_values=redacted_values,
                terminal_quota_fallback=True,
            )

    def _ingest_once(
        self,
        agent_id: str,
        raw_events: list[dict[str, Any]],
        *,
        redacted_values: set[str] | None,
        terminal_quota_fallback: bool,
    ) -> dict[str, Any]:
        """Execute one transactional batch admission attempt."""

        canonical: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_events:
            event = self.validate_event(raw)
            if event["agent_id"] != agent_id:
                raise RunServiceError("Agent token cannot submit events for another Agent")
            if event["type"] in {"kitsune.usage", "kitsune.model.usage"}:
                from kitsune_contracts import UsageRecord as UsageContract

                try:
                    usage = UsageContract.model_validate(event.get("payload", {}))
                except ValueError as exc:
                    raise RunServiceError(
                        f"Event {event['event_id']} has an invalid Usage payload"
                    ) from exc
                event["payload"] = usage.model_dump(mode="json", exclude_none=True)
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            canonical.append(event)
        inserted = 0
        duplicates: list[str] = []
        telemetry_updates: list[tuple[str, int | float, dict[str, str]]] = []
        with self.database.session() as session:
            definition = lock_agent_definition(session, agent_id)
            if definition is None or not definition.active:
                raise RunServiceError(f"unknown Agent Definition: {agent_id}")
            storage_usage = self.storage.lock_usage(session, agent_id)
            known_secrets = _resolved_persistence_secrets(
                self.settings,
                definition.snapshot,
                redacted_values,
            )
            self.telemetry.register_secrets(*known_secrets)
            records: list[Event] = []
            per_run_new: dict[str, int] = {}
            per_run_nonterminal: dict[str, bool] = {}
            batch_run_trace_ids: dict[str, str | None] = {}
            terminal_run_ids: set[str] = set()
            terminal_headroom_uses: dict[str, int] = {}
            reserved_payload_bytes = 0
            for item in canonical:
                referenced_run: Run | None = None
                if session.get(Event, item["event_id"]) is not None:
                    duplicates.append(item["event_id"])
                    continue
                runtime_id = item.get("runtime_instance_id")
                if runtime_id:
                    instance = session.get(RuntimeInstance, runtime_id)
                    if instance is None or instance.agent_id != agent_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} references an out-of-scope Runtime Instance"
                        )
                run_id = item.get("run_id")
                if run_id:
                    referenced_run = session.get(Run, run_id)
                    if referenced_run is None or referenced_run.agent_id != agent_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} references an out-of-scope Run"
                        )
                    if runtime_id is None or runtime_id != referenced_run.runtime_instance_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} Runtime Instance does not match "
                            "the Run assignment"
                        )
                    if item.get("parent_run_id") != referenced_run.parent_run_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} parent_run_id does not match the Run"
                        )
                    if item.get("correlation_id") != referenced_run.correlation_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} correlation_id does not match the Run"
                        )
                    event_trace_id = item.get("trace_id")
                    if referenced_run.trace_id is not None:
                        expected_trace_id = referenced_run.trace_id
                    elif run_id in batch_run_trace_ids:
                        expected_trace_id = batch_run_trace_ids[run_id]
                    else:
                        expected_trace_id = event_trace_id
                        batch_run_trace_ids[run_id] = expected_trace_id
                    if event_trace_id != expected_trace_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} trace_id does not match the Run"
                        )
                    per_run_nonterminal[run_id] = referenced_run.status not in TERMINAL_RUN_STATUSES
                elif item.get("parent_run_id"):
                    parent = session.get(Run, item["parent_run_id"])
                    if parent is None or parent.agent_id != agent_id:
                        raise RunServiceError(
                            f"Event {item['event_id']} references an out-of-scope parent Run"
                        )
                occurred_at = _normalise_datetime(item["occurred_at"])
                if occurred_at is None:
                    raise RunServiceError("event occurred_at is required")
                payload = redact(item.get("payload", {}), self.settings.security.redacted_keys)
                payload = redact_sensitive_data(
                    payload,
                    frozenset(self.settings.security.redacted_keys),
                    frozenset(known_secrets),
                )
                payload = redact_values(payload, known_secrets)
                if referenced_run is not None:
                    payload = _apply_run_event_persistence_policy(payload, referenced_run)
                terminal_event = item["type"] in {
                    "kitsune.run.succeeded",
                    "kitsune.run.completed",
                    "kitsune.run.failed",
                    "kitsune.run.cancelled",
                    "kitsune.run.timed_out",
                }
                terminal_transition = bool(
                    referenced_run is not None
                    and referenced_run.status not in TERMINAL_RUN_STATUSES
                    and referenced_run.run_id not in terminal_run_ids
                    and terminal_event
                )
                if terminal_quota_fallback and terminal_transition:
                    payload = {"persistence_rejected": {"reason": "agent_storage_quota"}}
                terminal_success = item["type"] in {
                    "kitsune.run.succeeded",
                    "kitsune.run.completed",
                }
                if referenced_run is not None and terminal_success and "output" in payload:
                    output_size = json_size(payload["output"])
                    if output_size > self.settings.events.max_output_bytes:
                        payload.pop("output", None)
                        payload["output_rejected"] = {
                            "reason": "too_large",
                            "size_bytes": output_size,
                        }
                payload_size = json_size(payload)
                if payload_size > self.settings.events.max_payload_bytes:
                    payload = {
                        "payload_rejected": {
                            "reason": "too_large",
                            "size_bytes": payload_size,
                        }
                    }
                    if referenced_run is not None and terminal_success:
                        payload["output_rejected"] = {
                            "reason": "too_large",
                            "size_bytes": payload_size,
                            "scope": "event_payload",
                        }
                record = Event(
                    event_id=item["event_id"],
                    type=item["type"],
                    occurred_at=occurred_at,
                    received_at=utcnow(),
                    agent_id=item["agent_id"],
                    runtime_instance_id=item.get("runtime_instance_id"),
                    run_id=item.get("run_id"),
                    parent_run_id=item.get("parent_run_id"),
                    correlation_id=item.get("correlation_id"),
                    trace_id=item.get("trace_id"),
                    severity=item.get("severity", "info"),
                    payload=payload,
                    storage_charge_bytes=compact_json_charge(payload),
                )
                records.append(record)
                terminal_headroom = (
                    min(
                        RUN_TERMINAL_EVENT_HEADROOM_BYTES,
                        record.storage_charge_bytes,
                    )
                    if terminal_transition
                    else 0
                )
                if terminal_headroom:
                    terminal_run_ids.add(record.run_id or "")
                    terminal_headroom_uses[record.event_id] = terminal_headroom
                reserved_payload_bytes += record.storage_charge_bytes - terminal_headroom
                if record.type in {"kitsune.usage", "kitsune.model.usage"}:
                    reserved_payload_bytes += compact_json_charge(payload)
                if record.run_id:
                    per_run_new[record.run_id] = per_run_new.get(record.run_id, 0) + 1
            for run_id, new_count in per_run_new.items():
                retained_count = (
                    session.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.agent_id == agent_id, Event.run_id == run_id)
                    )
                    or 0
                )
                reserved_terminal_slot = int(
                    per_run_nonterminal.get(run_id, False) and run_id not in terminal_run_ids
                )
                if (
                    retained_count + new_count + reserved_terminal_slot
                    > self.settings.events.max_events_per_run
                ):
                    raise StorageQuotaExceeded("Run retained Event count quota exceeded")
            active_runs = (
                session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.agent_id == agent_id,
                        Run.status.not_in(TERMINAL_RUN_STATUSES),
                    )
                )
                or 0
            )
            if (
                int(storage_usage.retained_event_count)
                + len(records)
                + active_runs
                - len(terminal_run_ids)
                > self.settings.events.max_events_per_agent
            ):
                raise StorageQuotaExceeded("Agent retained Event count quota exceeded")
            self.storage.reserve(
                storage_usage,
                events=len(records),
                payload_bytes=reserved_payload_bytes,
            )
            for record in records:
                session.add(record)
                self._apply_event(
                    session,
                    record,
                    storage_usage,
                    telemetry_updates,
                    terminal_headroom_used=terminal_headroom_uses.get(record.event_id, 0),
                    terminal_quota_fallback=terminal_quota_fallback,
                )
            inserted = len(records)
        self._emit_telemetry(telemetry_updates)
        return {"accepted": inserted, "duplicates": duplicates}

    def recover_outbox(self, agent_id: str, path: Path) -> int:
        """Persist and acknowledge SDK outbox Events in bounded admission batches."""

        if not path.is_file():
            return 0
        recovered = 0
        batch_size = 500
        recovery_batch_bytes = (
            max(
                self.settings.events.max_payload_bytes,
                self.settings.events.max_input_bytes,
                self.settings.events.max_output_bytes,
            )
            + 65_536
        )
        maximum_serialized_event_bytes = recovery_batch_bytes
        while True:
            with sqlite3.connect(path, timeout=30) as connection:
                try:
                    headers = connection.execute(
                        "SELECT event_id, length(CAST(payload AS BLOB)) "
                        "FROM event_outbox ORDER BY created_at, event_id LIMIT ?",
                        (batch_size,),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
                    return recovered
                if not headers:
                    return recovered
                if any(int(row[1]) > maximum_serialized_event_bytes for row in headers):
                    raise RunServiceError("Recovered outbox Event exceeds the configured limit")
                event_ids: list[str] = []
                selected_bytes = 0
                for event_id, payload_bytes in headers:
                    size = int(payload_bytes)
                    if event_ids and selected_bytes + size > recovery_batch_bytes:
                        break
                    event_ids.append(str(event_id))
                    selected_bytes += size
                payload_rows = [
                    connection.execute(
                        "SELECT event_id, payload FROM event_outbox WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    for event_id in event_ids
                ]
                if any(row is None for row in payload_rows):
                    raise RunServiceError("Recovered outbox changed during a recovery batch")
            events = [json.loads(str(row[1])) for row in payload_rows]
            self.ingest(agent_id, events)
            with sqlite3.connect(path, timeout=30) as connection:
                connection.executemany(
                    "DELETE FROM event_outbox WHERE event_id = ?",
                    [(event_id,) for event_id in event_ids],
                )
            recovered += len(event_ids)

    def _apply_event(
        self,
        session: Session,
        event: Event,
        storage_usage: AgentStorageUsage,
        telemetry_updates: list[tuple[str, int | float, dict[str, str]]],
        *,
        terminal_headroom_used: int,
        terminal_quota_fallback: bool,
    ) -> None:
        if event.runtime_instance_id:
            instance = session.get(RuntimeInstance, event.runtime_instance_id)
            if (
                instance
                and event.type in {"kitsune.agent.ready", "kitsune.runtime.ready"}
                and instance.stopped_at is None
                and instance.status in {"pending", "starting", "ready", "unhealthy"}
            ):
                if instance.status != "ready" or instance.ready_at is None:
                    instance.ready_at = event.received_at
                instance.status = "ready"
                instance.last_heartbeat_at = event.received_at
        if event.run_id:
            run = session.get(Run, event.run_id)
            if run:
                status_map = {
                    "kitsune.run.started": "running",
                    "kitsune.run.succeeded": "succeeded",
                    "kitsune.run.completed": "succeeded",
                    "kitsune.run.failed": "failed",
                    "kitsune.run.cancelled": "cancelled",
                    "kitsune.run.timed_out": "timed_out",
                }
                target = status_map.get(event.type)
                output_rejected = (event.payload or {}).get("output_rejected")
                oversized_output = bool(
                    target == "succeeded"
                    and isinstance(output_rejected, dict)
                    and output_rejected.get("reason") == "too_large"
                )
                if oversized_output:
                    target = "failed"
                was_terminal = run.status in TERMINAL_RUN_STATUSES
                transitioned = False
                if target and run.status != target and not was_terminal:
                    _transition(run, target, event.received_at)
                    transitioned = True
                    if terminal_headroom_used:
                        if int(run.storage_charge_bytes) < terminal_headroom_used:
                            raise RuntimeError("Run terminal Event headroom accounting underflow")
                        run.storage_charge_bytes = (
                            int(run.storage_charge_bytes) - terminal_headroom_used
                        )
                    if target in TERMINAL_RUN_STATUSES and run.started_at:
                        duration = (
                            ensure_aware(event.received_at) - ensure_aware(run.started_at)
                        ).total_seconds()
                        telemetry_updates.append(
                            (
                                "run_duration",
                                max(0.0, duration),
                                {
                                    "agent_id": run.agent_id,
                                    "handler": run.handler,
                                    "status": target,
                                },
                            )
                        )
                    if target == "failed":
                        telemetry_updates.append(("run_failures", 1, {"agent_id": run.agent_id}))
                payload = event.payload or {}
                if transitioned and target == "succeeded" and "output" in payload:
                    if run.store_output:
                        self.storage.reserve_run_field_change(
                            storage_usage,
                            run,
                            previous=run.output,
                            replacement=payload["output"],
                        )
                        run.output = payload["output"]
                if transitioned and oversized_output:
                    error = {
                        "type": "output_too_large",
                        "message": "Agent output exceeded the Workspace persistence limit",
                        "retryable": False,
                        "details": {},
                    }
                    self.storage.set_internal_run_error(run, error)
                    run.output = None
                elif transitioned and target == "failed":
                    error = payload.get("error")
                    if isinstance(error, dict):
                        persisted_error = {
                            "type": str(error.get("type") or error.get("code") or "run_failed"),
                            "message": str(error.get("message") or "Run failed"),
                            "retryable": bool(error.get("retryable", False)),
                            "details": (
                                error.get("details")
                                if isinstance(error.get("details"), dict)
                                else {}
                            ),
                        }
                    else:
                        persisted_error = {
                            "type": "run_failed",
                            "message": str(error or "Run failed"),
                            "retryable": False,
                            "details": {},
                        }
                    if terminal_quota_fallback:
                        self.storage.set_internal_run_error(run, persisted_error)
                    else:
                        self.storage.reserve_run_field_change(
                            storage_usage,
                            run,
                            previous=run.error,
                            replacement=persisted_error,
                        )
                        run.error = persisted_error
                if not was_terminal and run.trace_id is None and event.trace_id:
                    run.trace_id = event.trace_id
        if event.type in {"kitsune.usage", "kitsune.model.usage"}:
            self._persist_usage(session, event, telemetry_updates)

    def _persist_usage(
        self,
        session: Session,
        event: Event,
        telemetry_updates: list[tuple[str, int | float, dict[str, str]]],
    ) -> None:
        payload = event.payload or {}
        attributes = {"agent_id": event.agent_id}
        if payload.get("request_count") is not None:
            telemetry_updates.append(("model_requests", int(payload["request_count"]), attributes))
        if payload.get("input_tokens") is not None:
            telemetry_updates.append(
                ("model_input_tokens", int(payload["input_tokens"]), attributes)
            )
        if payload.get("output_tokens") is not None:
            telemetry_updates.append(
                ("model_output_tokens", int(payload["output_tokens"]), attributes)
            )
        if payload.get("estimated_cost") is not None:
            telemetry_updates.append(
                ("model_estimated_cost", float(payload["estimated_cost"]), attributes)
            )
        session.add(
            UsageRecord(
                run_id=event.run_id,
                agent_id=event.agent_id,
                event_id=event.event_id,
                provider=payload.get("provider"),
                model=payload.get("model"),
                request_count=payload.get("request_count"),
                input_tokens=payload.get("input_tokens"),
                output_tokens=payload.get("output_tokens"),
                total_tokens=payload.get("total_tokens"),
                cache_read_tokens=payload.get("cache_read_tokens"),
                cache_write_tokens=payload.get("cache_write_tokens"),
                estimated_cost=payload.get("estimated_cost"),
                currency=payload.get("currency"),
                recorded_at=event.received_at,
                raw=payload,
                storage_charge_bytes=compact_json_charge(payload),
            )
        )

    def _emit_telemetry(self, updates: list[tuple[str, int | float, dict[str, str]]]) -> None:
        """Emit counters and histograms only after the event transaction commits."""

        for instrument_name, value, attributes in updates:
            instrument = getattr(self.telemetry, instrument_name)
            if instrument_name == "run_duration":
                instrument.record(value, attributes)
            else:
                instrument.add(value, attributes)


class RetentionService:
    """Delete expired operational records while retaining audit longer."""

    def __init__(self, database: Database, settings: WorkspaceSettings) -> None:
        self.database = database
        self.settings = settings
        self.storage = StorageQuotaService(settings)

    def run(self, at: datetime | None = None) -> dict[str, int]:
        """Run one deterministic retention pass in short, bounded transactions."""

        now = at or utcnow()
        event_cutoff = now - timedelta(days=self.settings.events.retention_days)
        audit_cutoff = now - timedelta(days=self.settings.events.audit_retention_days)
        runtime_cutoff = now - timedelta(days=self.settings.events.runtime_retention_days)
        counts = {
            "events": 0,
            "usage_records": 0,
            "audit_records": 0,
            "runtime_instances": 0,
            "runs": 0,
        }
        with self.database.session() as session:
            agent_ids = sorted(session.scalars(select(AgentDefinition.agent_id)).all())
        for agent_id in agent_ids:
            counts["events"] += self._delete_event_chunks(
                agent_id,
                Event.run_id.is_(None),
                Event.received_at < event_cutoff,
            )
            counts["usage_records"] += self._delete_usage_chunks(
                agent_id,
                UsageRecord.run_id.is_(None),
                UsageRecord.recorded_at < event_cutoff,
            )
            cursor: str | None = None
            while True:
                with self.database.session() as session:
                    query = (
                        select(Run.run_id, Run.ended_at, Run.retention_days)
                        .where(
                            Run.agent_id == agent_id,
                            Run.status.in_(TERMINAL_RUN_STATUSES),
                            Run.ended_at.is_not(None),
                        )
                        .order_by(Run.run_id)
                        .limit(self.settings.events.retention_batch_size)
                    )
                    if cursor is not None:
                        query = query.where(Run.run_id > cursor)
                    candidates = list(session.execute(query).all())
                if not candidates:
                    break
                cursor = str(candidates[-1].run_id)
                expired_run_ids = [
                    str(candidate.run_id)
                    for candidate in candidates
                    if ensure_aware(candidate.ended_at)
                    < now
                    - timedelta(
                        days=(candidate.retention_days or self.settings.events.retention_days)
                    )
                ]
                if not expired_run_ids:
                    continue
                counts["events"] += self._delete_event_chunks(
                    agent_id, Event.run_id.in_(expired_run_ids)
                )
                counts["usage_records"] += self._delete_usage_chunks(
                    agent_id, UsageRecord.run_id.in_(expired_run_ids)
                )
                counts["runs"] += self._delete_run_chunk(agent_id, expired_run_ids)
            counts["runtime_instances"] += self._delete_runtime_chunks(agent_id, runtime_cutoff)
        counts["audit_records"] = self._delete_audit_chunks(audit_cutoff)
        return counts

    def _delete_event_chunks(self, agent_id: str, *predicates: Any) -> int:
        removed = 0
        while True:
            with self.database.session() as session:
                definition = lock_agent_definition(session, agent_id)
                if definition is None:
                    return removed
                storage_usage = self.storage.lock_usage(session, agent_id)
                headers = list(
                    session.execute(
                        select(Event.event_id, Event.storage_charge_bytes)
                        .where(Event.agent_id == agent_id, *predicates)
                        .order_by(Event.received_at, Event.event_id)
                        .limit(self.settings.events.retention_batch_size)
                    ).all()
                )
                selected = self._bounded_charge_prefix(headers)
                if not selected:
                    return removed
                event_ids = [str(item[0]) for item in selected]
                deleted = _affected_rows(
                    session.execute(
                        delete(Event)
                        .where(Event.event_id.in_(event_ids))
                        .execution_options(synchronize_session=False)
                    )
                )
                if deleted != len(event_ids):
                    raise RuntimeError("Event retention delete count changed while Agent locked")
                self.storage.release(
                    storage_usage,
                    events=deleted,
                    payload_bytes=sum(int(item[1]) for item in selected),
                )
                removed += deleted

    def _delete_usage_chunks(self, agent_id: str, *predicates: Any) -> int:
        removed = 0
        while True:
            with self.database.session() as session:
                definition = lock_agent_definition(session, agent_id)
                if definition is None:
                    return removed
                storage_usage = self.storage.lock_usage(session, agent_id)
                headers = list(
                    session.execute(
                        select(UsageRecord.id, UsageRecord.storage_charge_bytes)
                        .where(UsageRecord.agent_id == agent_id, *predicates)
                        .order_by(UsageRecord.recorded_at, UsageRecord.id)
                        .limit(self.settings.events.retention_batch_size)
                    ).all()
                )
                selected = self._bounded_charge_prefix(headers)
                if not selected:
                    return removed
                record_ids = [int(item[0]) for item in selected]
                deleted = _affected_rows(
                    session.execute(
                        delete(UsageRecord)
                        .where(UsageRecord.id.in_(record_ids))
                        .execution_options(synchronize_session=False)
                    )
                )
                if deleted != len(record_ids):
                    raise RuntimeError("Usage retention delete count changed while Agent locked")
                self.storage.release(
                    storage_usage,
                    payload_bytes=sum(int(item[1]) for item in selected),
                )
                removed += deleted

    def _delete_run_chunk(self, agent_id: str, run_ids: list[str]) -> int:
        """Delete only expired Runs whose linked rows are gone in the same locked view."""

        with self.database.session() as session:
            definition = lock_agent_definition(session, agent_id)
            if definition is None:
                return 0
            storage_usage = self.storage.lock_usage(session, agent_id)
            linked_event = select(Event.event_id).where(Event.run_id == Run.run_id).exists()
            linked_usage = select(UsageRecord.id).where(UsageRecord.run_id == Run.run_id).exists()
            headers = list(
                session.execute(
                    select(Run.run_id, Run.storage_charge_bytes)
                    .where(
                        Run.agent_id == agent_id,
                        Run.run_id.in_(run_ids),
                        ~linked_event,
                        ~linked_usage,
                    )
                    .order_by(Run.run_id)
                    .limit(self.settings.events.retention_batch_size)
                ).all()
            )
            selected = self._bounded_charge_prefix(headers)
            run_ids_to_delete = [str(item[0]) for item in selected]
            deleted = 0
            if run_ids_to_delete:
                deleted = _affected_rows(
                    session.execute(
                        delete(Run)
                        .where(Run.run_id.in_(run_ids_to_delete))
                        .execution_options(synchronize_session=False)
                    )
                )
                if deleted != len(run_ids_to_delete):
                    raise RuntimeError("Run retention delete count changed while Agent locked")
                self.storage.release(
                    storage_usage,
                    runs=deleted,
                    payload_bytes=sum(int(item[1]) for item in selected),
                )
            return deleted

    def _delete_runtime_chunks(self, agent_id: str, cutoff: datetime) -> int:
        removed = 0
        while True:
            with self.database.session() as session:
                definition = lock_agent_definition(session, agent_id)
                if definition is None:
                    return removed
                storage_usage = self.storage.lock_usage(session, agent_id)
                record_ids = list(
                    session.scalars(
                        select(RuntimeInstance.runtime_instance_id)
                        .where(
                            RuntimeInstance.agent_id == agent_id,
                            RuntimeInstance.status.in_(["stopped", "failed", "lost"]),
                            RuntimeInstance.stopped_at.is_not(None),
                            RuntimeInstance.stopped_at < cutoff,
                            ~RuntimeInstance.runtime_instance_id.in_(
                                select(Run.runtime_instance_id).where(
                                    Run.runtime_instance_id.is_not(None)
                                )
                            ),
                        )
                        .order_by(
                            RuntimeInstance.stopped_at,
                            RuntimeInstance.runtime_instance_id,
                        )
                        .limit(self.settings.events.retention_batch_size)
                    )
                )
                if not record_ids:
                    return removed
                deleted = _affected_rows(
                    session.execute(
                        delete(RuntimeInstance)
                        .where(RuntimeInstance.runtime_instance_id.in_(record_ids))
                        .execution_options(synchronize_session=False)
                    )
                )
                if deleted != len(record_ids):
                    raise RuntimeError("Runtime retention delete count changed while Agent locked")
                self.storage.release(storage_usage, runtimes=deleted)
                removed += deleted

    def _delete_audit_chunks(self, cutoff: datetime) -> int:
        removed = 0
        while True:
            with self.database.session() as session:
                record_ids = list(
                    session.scalars(
                        select(AuditRecord.id)
                        .where(AuditRecord.occurred_at < cutoff)
                        .order_by(AuditRecord.occurred_at, AuditRecord.id)
                        .limit(self.settings.events.retention_batch_size)
                    )
                )
                if not record_ids:
                    return removed
                removed += _affected_rows(
                    session.execute(delete(AuditRecord).where(AuditRecord.id.in_(record_ids)))
                )

    def _bounded_charge_prefix(self, rows: list[Any]) -> list[Any]:
        """Select a stable count-and-byte-bounded deletion prefix without loading JSON."""

        selected: list[Any] = []
        selected_bytes = 0
        for row in rows:
            charge = int(row[1])
            if selected and selected_bytes + charge > self.settings.events.retention_batch_bytes:
                break
            selected.append(row)
            selected_bytes += charge
        return selected


def _affected_rows(result: Any) -> int:
    """Read DML row counts across SQLAlchemy dialect result implementations."""

    return max(0, int(getattr(result, "rowcount", 0) or 0))


def serialize_runtime(instance: RuntimeInstance) -> dict[str, Any]:
    """Serialize a Runtime Instance for the management API."""

    return {
        "runtime_instance_id": instance.runtime_instance_id,
        "agent_id": instance.agent_id,
        "adapter": instance.adapter,
        "mode": instance.mode,
        "status": instance.status,
        "pid": instance.pid,
        "container_id": instance.container_id,
        "endpoint": instance.control_url,
        "control_url": instance.control_url,
        "health_url": instance.health_url,
        "log_url": instance.log_url,
        "trace_url": instance.trace_url,
        "restart_attempts": instance.restart_attempts,
        "started_at": instance.started_at,
        "ready_at": instance.ready_at,
        "stopped_at": instance.stopped_at,
        "last_heartbeat_at": instance.last_heartbeat_at,
        "last_exit_code": instance.last_exit_code,
        "last_error": instance.last_error,
        "metadata": instance.runtime_metadata,
    }


def serialize_run(run: Run, usage: list[UsageRecord] | None = None) -> dict[str, Any]:
    """Serialize one Run without inventing unavailable usage fields."""

    result = {
        "run_id": run.run_id,
        "agent_id": run.agent_id,
        "runtime_instance_id": run.runtime_instance_id,
        "handler": run.handler,
        "source": run.source,
        "trigger_id": run.trigger_id,
        "parent_run_id": run.parent_run_id,
        "correlation_id": run.correlation_id,
        "trace_id": run.trace_id,
        "status": run.status,
        "input": run.input,
        "output": run.output,
        "created_at": run.created_at,
        "queued_at": run.queued_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "deadline": run.deadline,
        "error": run.error,
        "log_url": run.log_url,
        "trace_url": run.trace_url,
    }
    result["usage"] = [serialize_usage(item) for item in usage or []]
    return result


def serialize_usage(item: UsageRecord) -> dict[str, Any]:
    """Serialize only provider-reported usage values."""

    return {
        "id": item.id,
        "run_id": item.run_id,
        "agent_id": item.agent_id,
        "provider": item.provider,
        "model": item.model,
        "request_count": item.request_count,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "total_tokens": item.total_tokens,
        "cache_read_tokens": item.cache_read_tokens,
        "cache_write_tokens": item.cache_write_tokens,
        "estimated_cost": item.estimated_cost,
        "currency": item.currency,
        "recorded_at": item.recorded_at,
    }
