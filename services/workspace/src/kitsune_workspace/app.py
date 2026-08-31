"""FastAPI application exposing management, Agent, webhook, SSE, and static UI routes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import signal
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from kitsune.logging import is_sensitive_key, redact_sensitive_data, redact_sensitive_text
from kitsune_contracts import (
    AGENT_DESCRIPTOR_MAX_BYTES,
    AgentHeartbeat,
    AgentRegistration,
    AgentRunAcknowledgement,
    AgentRunAssignment,
    AgentRunBegin,
    EventBatch,
)
from kitsune_contracts import AgentDescriptor as AgentDescriptorContract
from pydantic import ValidationError
from sqlalchemy import func, select, text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api_models import (
    AgentDetail,
    AgentSummary,
    AuditView,
    AuthMeResponse,
    DashboardResponse,
    ErrorResponse,
    EventBatchResponse,
    EventView,
    HandlerView,
    LogsResponse,
    ManifestReloadErrorResponse,
    ManifestReloadResponse,
    OperationResponse,
    RegistrationResponse,
    RunCreateRequest,
    RuntimeView,
    RunView,
    ScheduleView,
    TokenIssueRequest,
    TokenIssueResponse,
    TokenView,
    UsageView,
    WorkspaceHealthResponse,
)
from .config import WorkspaceSettings, resolve_secret_reference
from .control_plane import ControlPlane, EventStreamLimitExceeded
from .database import Database
from .manifest import ManifestReloadError
from .models import (
    AgentCredential,
    AgentDefinition,
    AuditRecord,
    Event,
    Handler,
    Run,
    RuntimeInstance,
    Schedule,
    Trigger,
    UsageRecord,
)
from .models import (
    AgentDescriptor as DescriptorRecord,
)
from .runtime import RuntimeOperationError
from .security import (
    FixedWindowRateLimiter,
    OIDCManager,
    Principal,
    agent_authorization,
    issue_agent_token,
    principal_dependency,
    require_role,
    verify_agent_access,
)
from .services import (
    InvalidRunTransition,
    QueueCapacityExceeded,
    RunServiceError,
    _resolved_persistence_secrets,
    _transition,
    audit,
    serialize_run,
    serialize_runtime,
    serialize_usage,
)
from .storage import StorageQuotaExceeded, lock_agent_definition
from .util import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES, ensure_aware, nested, redact, utcnow

_SCHEMA_DEFINITION_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_VALUE_KEYS = frozenset({"const", "default", "enum", "example", "examples"})

_DOCUMENTED_ERROR_STATUSES: dict[str, frozenset[int]] = {
    "/api/auth/logout": frozenset({401, 403, 429}),
    "/api/auth/me": frozenset({401, 429}),
    "/api/dashboard": frozenset({401, 403, 429}),
    "/api/agents": frozenset({401, 403, 429}),
    "/api/agents/{agent_id}": frozenset({401, 403, 404, 429}),
    "/api/agents/{agent_id}/runtime-instances": frozenset({401, 403, 429}),
    "/api/agents/{agent_id}/handlers": frozenset({401, 403, 429}),
    "/api/agents/{agent_id}/start": frozenset({401, 403, 404, 409, 429, 503}),
    "/api/agents/{agent_id}/stop": frozenset({401, 403, 404, 409, 429, 503}),
    "/api/agents/{agent_id}/restart": frozenset({401, 403, 404, 409, 429, 503}),
    "/api/agents/{agent_id}/runs": frozenset({401, 403, 429, 503}),
    "/api/runs": frozenset({401, 403, 429}),
    "/api/runs/{run_id}": frozenset({401, 403, 404, 429}),
    "/api/runs/{run_id}/cancel": frozenset({401, 403, 429, 503}),
    "/api/runs/{run_id}/events": frozenset({401, 403, 429}),
    "/api/runs/{run_id}/usage": frozenset({401, 403, 429}),
    "/api/schedules": frozenset({401, 403, 429}),
    "/api/audit": frozenset({401, 403, 429}),
    "/api/runtime-instances/{runtime_instance_id}/logs": frozenset({401, 403, 404, 409, 429}),
    "/api/health": frozenset({401, 403, 429}),
    "/api/stream": frozenset({401, 403, 429, 503}),
    "/api/admin/reload": frozenset({401, 403, 429, 503}),
    "/api/admin/tokens": frozenset({401, 403, 404, 429}),
    "/api/admin/tokens/{credential_id}": frozenset({401, 403, 404, 429, 503}),
    "/api/agent/register": frozenset({401, 404, 409, 429, 503}),
    "/api/agent/heartbeat": frozenset({401, 403, 404, 409, 429, 503}),
    "/api/agent/events/batch": frozenset({401, 429, 503}),
    "/api/agent/runs/begin": frozenset({401, 429, 503}),
    "/api/agent/runs/{run_id}/input": frozenset({401, 404, 429}),
    "/api/agent/runs/{run_id}/ack": frozenset({401, 404, 409, 429, 503}),
    "/hooks/{agent_id}/{trigger_id}": frozenset({400, 401, 404, 413, 429, 503}),
}


def _install_openapi_contract(app: FastAPI, *, session_cookie_name: str) -> None:
    """Add shared error and conditional browser-header metadata to route contracts."""

    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original_openapi()
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas.setdefault("ErrorResponse", ErrorResponse.model_json_schema())
        schemas.setdefault(
            "ManifestReloadErrorResponse", ManifestReloadErrorResponse.model_json_schema()
        )
        schema["components"]["securitySchemes"]["SessionCookie"]["name"] = session_cookie_name
        error_response = {
            "description": "FastAPI error response",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        }
        for path, operations in schema.get("paths", {}).items():
            statuses = _DOCUMENTED_ERROR_STATUSES.get(path, frozenset())
            for method, operation in operations.items():
                if not isinstance(operation, dict):
                    continue
                operation_statuses = (
                    statuses - {404}
                    if path == "/api/admin/tokens" and method == "get"
                    else statuses
                )
                for status_code in operation_statuses:
                    operation.setdefault("responses", {}).setdefault(
                        str(status_code), error_response.copy()
                    )
                validation_response = operation.get("responses", {}).get("422")
                if validation_response is not None:
                    validation_content = validation_response.setdefault("content", {})
                    json_content = validation_content.setdefault("application/json", {})
                    json_content["schema"] = {
                        "oneOf": [
                            {"$ref": "#/components/schemas/HTTPValidationError"},
                            {"$ref": "#/components/schemas/ErrorResponse"},
                        ]
                    }
                if path == "/api/admin/reload" and method == "post":
                    operation.setdefault("responses", {})["422"] = {
                        "description": "Manifest reload validation failed",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ManifestReloadErrorResponse"
                                }
                            }
                        },
                    }
                if (
                    path.startswith("/api/")
                    and method in {"post", "put", "patch", "delete"}
                    and "agent" not in operation.get("tags", [])
                ):
                    parameters = operation.setdefault("parameters", [])
                    parameters[:] = [
                        parameter
                        for parameter in parameters
                        if not (
                            parameter.get("in") == "header"
                            and parameter.get("name", "").lower() == "x-csrf-token"
                        )
                    ]
                    parameters.append(
                        {
                            "name": "X-CSRF-Token",
                            "in": "header",
                            "required": False,
                            "description": (
                                "Required when auth.mode=oidc for browser mutations; "
                                "ignored when auth.mode=none."
                            ),
                            "schema": {"type": "string"},
                        }
                    )
                if operation.get("security") == [{"SessionCookie": []}]:
                    operation["security"] = [{"SessionCookie": []}, {}]
        return schema

    app.openapi = openapi


def _sanitize_schema_free_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_schema_free_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_schema_free_value(item) for item in value]
    return "[REDACTED]"


def _sanitize_descriptor_schema(
    value: Any,
    redacted_keys: frozenset[str],
    redacted_values: frozenset[str],
    *,
    sensitive_definition: bool = False,
) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value, redacted_keys, redacted_values)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _SCHEMA_DEFINITION_MAP_KEYS and isinstance(item, dict):
                sanitized[key] = {
                    str(name): _sanitize_descriptor_schema(
                        definition,
                        redacted_keys,
                        redacted_values,
                        sensitive_definition=(
                            sensitive_definition or is_sensitive_key(name, redacted_keys)
                        ),
                    )
                    for name, definition in item.items()
                }
            elif is_sensitive_key(key, redacted_keys):
                sanitized[key] = "[REDACTED]"
            elif sensitive_definition and key in _SCHEMA_VALUE_KEYS:
                sanitized[key] = _sanitize_schema_free_value(item)
            else:
                sanitized[key] = _sanitize_descriptor_schema(
                    item,
                    redacted_keys,
                    redacted_values,
                    sensitive_definition=sensitive_definition,
                )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_descriptor_schema(
                item,
                redacted_keys,
                redacted_values,
                sensitive_definition=sensitive_definition,
            )
            for item in value
        ]
    return value


def _sanitize_descriptor(
    descriptor: dict[str, Any],
    redacted_keys: frozenset[str],
    redacted_values: frozenset[str],
) -> dict[str, Any]:
    """Redact descriptor data while preserving JSON Schema definition structure."""

    ordinary = {key: value for key, value in descriptor.items() if key != "handlers"}
    sanitized = redact_sensitive_data(ordinary, redacted_keys, redacted_values)
    if not isinstance(sanitized, dict):  # pragma: no cover - fixed descriptor shape
        raise ValueError("Agent descriptor must be an object")
    sanitized_handlers: list[Any] = []
    for raw_handler in descriptor.get("handlers", []):
        if not isinstance(raw_handler, dict):
            sanitized_handlers.append(raw_handler)
            continue
        handler = {
            key: value
            for key, value in raw_handler.items()
            if key not in {"input_schema", "output_schema"}
        }
        sanitized_handler = redact_sensitive_data(handler, redacted_keys, redacted_values)
        if not isinstance(sanitized_handler, dict):  # pragma: no cover - fixed descriptor shape
            raise ValueError("Agent handler descriptor must be an object")
        for schema_key in ("input_schema", "output_schema"):
            if schema_key in raw_handler:
                sanitized_handler[schema_key] = _sanitize_descriptor_schema(
                    raw_handler[schema_key], redacted_keys, redacted_values
                )
        sanitized_handlers.append(sanitized_handler)
    sanitized["handlers"] = sanitized_handlers
    return sanitized


class RequestBodyLimitMiddleware:
    """Bound and replay request chunks before FastAPI routing or validation begins."""

    def __init__(self, app: ASGIApp, maximum: int) -> None:
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, raw_value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(raw_value)
            except ValueError:
                continue
            if declared > self.maximum:
                response = JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
                await response(scope, receive, send)
                return

        received = 0
        buffered_messages: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum:
                    response = JSONResponse(
                        status_code=413, content={"detail": "request body too large"}
                    )
                    await response(scope, receive, send)
                    return
            buffered_messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(buffered_messages):
                message = buffered_messages[message_index]
                message_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


def _canonical_origin(value: str, *, allow_path: bool = False) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or (not allow_path and parsed.path not in {"", "/"})
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    try:
        normalized_host = ipaddress.ip_address(host).compressed
    except ValueError:
        normalized_host = host.casefold()
    return parsed.scheme, normalized_host, port


def _loopback_authority(value: str, *, expected_port: int, scheme: str) -> bool:
    try:
        parsed = urlparse(f"//{value}")
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False
    if (
        host is None
        or port != expected_port
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
    ):
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LoopbackRequestBoundaryMiddleware:
    """Protect unauthenticated loopback mode from DNS rebinding and cross-site requests."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bind_port: int,
        trusted_origin: str,
    ) -> None:
        self.app = app
        self.bind_port = bind_port
        self.trusted_origin = _canonical_origin(trusted_origin, allow_path=True)
        if self.trusted_origin is None:
            raise ValueError("loopback mode requires a valid trusted Workspace origin")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host_values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"host"
        ]
        if len(host_values) != 1 or not _loopback_authority(
            host_values[0], expected_port=self.bind_port, scheme=scope.get("scheme", "http")
        ):
            await JSONResponse(status_code=400, content={"detail": "invalid Host header"})(
                scope, receive, send
            )
            return
        origin_values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"origin"
        ]
        if origin_values and (
            len(origin_values) != 1 or _canonical_origin(origin_values[0]) != self.trusted_origin
        ):
            await JSONResponse(status_code=403, content={"detail": "cross-origin request denied"})(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def _remote_address(request: Request) -> str | None:
    return request.client.host if request.client else None


def _agent_source_address(request: Request) -> str:
    """Return one bounded pre-auth Agent source key across a trusted local gateway."""

    remote = _remote_address(request)
    if remote is None:
        return "unknown"
    try:
        peer = ipaddress.ip_address(remote)
    except ValueError:
        return "unknown"
    forwarded = request.headers.get("X-Kitsune-Agent-Remote")
    if peer.is_loopback and forwarded:
        try:
            return str(ipaddress.ip_address(forwarded.strip()))
        except ValueError:
            return str(peer)
    return str(peer)


def _effective_role(principal: Principal) -> str:
    """Return the highest effective role represented by one authenticated principal."""

    for role in ("admin", "operator", "viewer"):
        if role in principal.roles:
            return role
    return "viewer"


def _schedule_view(schedule: Schedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "agent_id": schedule.agent_id,
        "trigger_id": schedule.trigger_id,
        "handler": schedule.handler,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "overlap": schedule.overlap,
        "misfire_grace_seconds": schedule.misfire_grace_seconds,
        "enabled": schedule.enabled,
        "next_fire_at": schedule.next_fire_at,
        "last_fire_at": schedule.last_fire_at,
        "last_outcome": schedule.last_outcome,
    }


def _run_assignment(run: Run) -> dict[str, Any]:
    """Serialize the shared immutable Agent Run assignment wire model."""

    return {
        "run_id": run.run_id,
        "agent_id": run.agent_id,
        "handler": run.handler,
        "source": run.source,
        "input": run.input,
        "parent_run_id": run.parent_run_id,
        "correlation_id": run.correlation_id,
        "trace_id": run.trace_id,
        "deadline": run.deadline,
    }


def _actual_state(instances: list[RuntimeInstance]) -> str:
    active = [
        instance.status
        for instance in instances
        if instance.status in {"pending", "starting", "ready", "unhealthy", "stopping"}
    ]
    for state in ("unhealthy", "starting", "pending", "ready", "stopping"):
        if state in active:
            return state
    return instances[0].status if instances else "stopped"


def _normalise_plugin(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {
            "name": raw,
            "version": None,
            "critical": False,
            "status": "reported",
            "error": None,
        }
    if not isinstance(raw, dict):
        return {
            "name": str(raw),
            "version": None,
            "critical": False,
            "status": "reported",
            "error": None,
        }
    return {
        "name": str(raw.get("name") or raw.get("id") or "unknown"),
        "version": raw.get("version"),
        "critical": bool(raw.get("critical", False)),
        "status": str(raw.get("status", "reported")),
        "error": raw.get("error"),
    }


def _agent_summary(session: Any, definition: AgentDefinition) -> dict[str, Any]:
    descriptor = session.scalar(
        select(DescriptorRecord).where(DescriptorRecord.agent_id == definition.agent_id)
    )
    instances = list(
        session.scalars(
            select(RuntimeInstance)
            .where(RuntimeInstance.agent_id == definition.agent_id)
            .order_by(RuntimeInstance.started_at.desc())
        )
    )
    last_heartbeat = max(
        (item.last_heartbeat_at for item in instances if item.last_heartbeat_at is not None),
        default=None,
    )
    active_runs = (
        session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.agent_id == definition.agent_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        )
        or 0
    )
    last_run_record = session.scalar(
        select(Run)
        .where(Run.agent_id == definition.agent_id)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    return {
        "agent_id": definition.agent_id,
        "display_name": definition.display_name,
        "description": definition.description,
        "version": descriptor.application_version if descriptor else None,
        "runtime_adapter": definition.runtime_adapter,
        "runtime_mode": definition.runtime_mode,
        "desired_state": definition.desired_state,
        "actual_state": _actual_state(instances),
        "last_heartbeat": last_heartbeat,
        "active_runs": active_runs,
        "last_run": (
            {
                "run_id": last_run_record.run_id,
                "agent_id": last_run_record.agent_id,
                "handler": last_run_record.handler,
                "source": last_run_record.source,
                "status": last_run_record.status,
                "created_at": last_run_record.created_at,
                "started_at": last_run_record.started_at,
                "ended_at": last_run_record.ended_at,
            }
            if last_run_record
            else None
        ),
        "labels": definition.labels,
    }


def _usage_summary(
    session: Any, *, agent_id: str | None = None, since: datetime | None = None
) -> dict[str, Any]:
    filters = []
    if agent_id:
        filters.append(UsageRecord.agent_id == agent_id)
    if since:
        filters.append(UsageRecord.recorded_at >= since)
    values = session.execute(
        select(
            func.coalesce(func.sum(UsageRecord.request_count), 0),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            func.coalesce(func.sum(UsageRecord.estimated_cost), Decimal("0")),
        ).where(*filters)
    ).one()
    currencies = list(
        session.scalars(
            select(UsageRecord.currency)
            .where(*filters, UsageRecord.currency.is_not(None))
            .distinct()
        )
    )
    return {
        "request_count": int(values[0]),
        "input_tokens": int(values[1]),
        "output_tokens": int(values[2]),
        "total_tokens": int(values[3]),
        "estimated_cost": (
            values[4] if isinstance(values[4], Decimal) else Decimal(str(values[4] or 0))
        ),
        "currency": currencies[0] if len(currencies) == 1 else None,
    }


def _event_view(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "type": event.type,
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
        "agent_id": event.agent_id,
        "runtime_instance_id": event.runtime_instance_id,
        "run_id": event.run_id,
        "parent_run_id": event.parent_run_id,
        "correlation_id": event.correlation_id,
        "trace_id": event.trace_id,
        "severity": event.severity,
        "payload": event.payload,
    }


def _audit_view(record: AuditRecord, redacted_keys: set[str]) -> dict[str, Any]:
    return {
        "id": record.id,
        "occurred_at": record.occurred_at,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "actor_role": record.actor_role,
        "request_id": record.request_id,
        "action": record.action,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "outcome": record.outcome,
        "remote_address": record.remote_address,
        "details": redact(record.details, redacted_keys),
    }


def create_app(settings: WorkspaceSettings | None = None) -> FastAPI:
    """Create the same typed application used by the server and OpenAPI exporter."""

    resolved = settings or WorkspaceSettings.load()
    database = Database(resolved.workspace.database_url)
    control = ControlPlane(database, resolved)
    oidc = OIDCManager(resolved.auth)
    rate_limiter = FixedWindowRateLimiter()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.create_schema()
        await control.start()
        loop = asyncio.get_running_loop()
        signal_installed = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGHUP, control.request_reload)
            signal_installed = True
        try:
            yield
        finally:
            if signal_installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(signal.SIGHUP)
            await control.stop()
            database.dispose()

    app = FastAPI(
        title="Kitsune Workspace API",
        version="1.0.0",
        description="Control plane for manifest-defined Agent Applications.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.database = database
    app.state.control = control
    app.state.oidc = oidc
    app.state.rate_limiter = rate_limiter

    def check_agent_rate_limit(agent_id: str) -> None:
        """Consume only the authenticated Agent's post-auth request budget."""

        rate_limiter.check(f"agent:{agent_id}", resolved.security.api_rate_limit)

    transport_body_maximum = (
        max(
            resolved.events.max_payload_bytes,
            resolved.events.max_input_bytes,
            resolved.events.max_output_bytes,
        )
        + 65_536
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum=transport_body_maximum,
    )
    if resolved.auth.mode == "none":
        bind_host = resolved.workspace.bind_host
        origin_host = f"[{bind_host}]" if ":" in bind_host else bind_host
        trusted_origin = resolved.workspace.public_url or (
            f"http://{origin_host}:{resolved.workspace.bind_port}"
        )
        app.add_middleware(
            LoopbackRequestBoundaryMiddleware,
            bind_port=resolved.workspace.bind_port,
            trusted_origin=trusted_origin,
        )

    @app.exception_handler(ManifestReloadError)
    async def manifest_error(_: Request, exc: ManifestReloadError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": "manifest reload rejected", "errors": exc.errors}
        )

    @app.exception_handler(QueueCapacityExceeded)
    async def queue_error(_: Request, exc: QueueCapacityExceeded) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(StorageQuotaExceeded)
    async def storage_quota_error(_: Request, exc: StorageQuotaExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(InvalidRunTransition)
    async def transition_error(_: Request, exc: InvalidRunTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(RunServiceError)
    async def run_error(_: Request, exc: RunServiceError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RuntimeOperationError)
    async def runtime_error(_: Request, exc: RuntimeOperationError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def boundary_middleware(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID")
        if (
            not request_id
            or len(request_id) > 64
            or not all(character.isalnum() or character in "-_." for character in request_id)
        ):
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        def finalize(response: Response) -> Response:
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; connect-src 'self'; frame-ancestors 'none'"
            )
            if request.url.path.startswith("/api/"):
                existing_cache_control = response.headers.get("Cache-Control", "")
                response.headers["Cache-Control"] = (
                    "no-store, no-cache" if "no-cache" in existing_cache_control else "no-store"
                )
                response.headers["Pragma"] = "no-cache"
            return response

        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path.startswith(("/api/", "/hooks/"))
            and not request.url.path.startswith("/api/auth/")
            and not control.accepting_operations
        ):
            return finalize(
                JSONResponse(
                    status_code=503,
                    content={"detail": "control-plane instance lock is not owned"},
                )
            )
        if request.url.path.startswith("/api/"):
            if request.url.path.startswith("/api/agent/"):
                key = f"agent-source:{_agent_source_address(request)}"
            else:
                key = f"api:{_remote_address(request) or 'unknown'}"
            try:
                rate_limiter.check(key, resolved.security.api_rate_limit)
            except HTTPException as exc:
                return finalize(
                    JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=exc.headers,
                    )
                )
        elif request.url.path.startswith("/hooks/"):
            try:
                rate_limiter.check(
                    f"webhook-source:{_remote_address(request) or 'unknown'}",
                    resolved.security.webhook_rate_limit,
                )
            except HTTPException as exc:
                return finalize(
                    JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=exc.headers,
                    )
                )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > transport_body_maximum:
                    return finalize(
                        JSONResponse(status_code=413, content={"detail": "request body too large"})
                    )
            except ValueError:
                return finalize(
                    JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
                )
        has_body = bool(request.headers.get("transfer-encoding"))
        if content_length:
            with contextlib.suppress(ValueError):
                has_body = has_body or int(content_length) > 0
        if (
            has_body
            and request.url.path.startswith("/api/")
            and "application/json" not in request.headers.get("content-type", "").casefold()
        ):
            return finalize(
                JSONResponse(
                    status_code=415,
                    content={"detail": "API request body must use application/json"},
                )
            )
        return finalize(await call_next(request))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        try:
            with database.session() as session:
                session.execute(text("SELECT 1"))
        except BaseException as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        if control.last_error or not control.accepting_operations:
            raise HTTPException(status_code=503, detail="control plane loop unhealthy")
        return {"status": "ready"}

    @app.get("/api/auth/login", include_in_schema=False)
    async def auth_login(request: Request) -> Response:
        if resolved.auth.mode != "oidc":
            return RedirectResponse(url="/")
        response = RedirectResponse(url="/", status_code=302)
        response.headers["location"] = await oidc.login(request, response)
        return response

    @app.get("/api/auth/callback", name="oidc_callback", include_in_schema=False)
    async def oidc_callback(request: Request) -> Response:
        if resolved.auth.mode != "oidc":
            raise HTTPException(status_code=404)
        response = RedirectResponse(url="/", status_code=302)
        response.headers["location"] = await oidc.callback(request, response)
        return response

    @app.post("/api/auth/logout", status_code=204, tags=["auth"])
    async def auth_logout(
        response: Response,
        _: Principal = Depends(require_role("viewer")),
    ) -> None:
        oidc.logout(response)

    @app.get("/api/auth/me", response_model=AuthMeResponse, tags=["auth"])
    async def auth_me(principal: Principal = Depends(principal_dependency)) -> dict[str, Any]:
        return {
            "subject": principal.subject,
            "name": principal.name,
            "roles": list(principal.roles),
            "csrf_token": principal.csrf_token,
        }

    @app.get("/api/dashboard", response_model=DashboardResponse, tags=["management"])
    async def dashboard(_: Principal = Depends(require_role("viewer"))) -> dict[str, Any]:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with database.session() as session:
            definitions = list(
                session.scalars(select(AgentDefinition).where(AgentDefinition.active.is_(True)))
            )
            states = [_agent_summary(session, item)["actual_state"] for item in definitions]
            active_runs = (
                session.scalar(
                    select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE_RUN_STATUSES))
                )
                or 0
            )
            failed_runs = (
                session.scalar(select(func.count()).select_from(Run).where(Run.status == "failed"))
                or 0
            )
            schedules = list(
                session.scalars(
                    select(Schedule)
                    .where(Schedule.enabled.is_(True))
                    .order_by(Schedule.next_fire_at)
                    .limit(10)
                )
            )
            abnormal_events = list(
                session.scalars(
                    select(Event)
                    .where(Event.severity.in_(["warning", "error", "critical"]))
                    .order_by(Event.occurred_at.desc())
                    .limit(10)
                )
            )
            anomalies = [
                {
                    "id": event.event_id,
                    "severity": event.severity,
                    "agent_id": event.agent_id,
                    "run_id": event.run_id,
                    "occurred_at": event.occurred_at,
                    "message": str((event.payload or {}).get("message") or event.type),
                }
                for event in abnormal_events
            ]
            return {
                "agents": {
                    "total": len(definitions),
                    "running": sum(state in {"ready", "starting", "pending"} for state in states),
                    "stopped": states.count("stopped"),
                    "failed": sum(state in {"failed", "unhealthy", "lost"} for state in states),
                },
                "runs": {"active": active_runs, "failed": failed_runs},
                "usage_today": _usage_summary(session, since=today),
                "recent_anomalies": anomalies,
                "next_schedules": [_schedule_view(item) for item in schedules],
            }

    @app.get("/api/agents", response_model=list[AgentSummary], tags=["management"])
    async def list_agents(_: Principal = Depends(require_role("viewer"))) -> list[dict[str, Any]]:
        with database.session() as session:
            definitions = list(
                session.scalars(
                    select(AgentDefinition)
                    .where(AgentDefinition.active.is_(True))
                    .order_by(AgentDefinition.agent_id)
                )
            )
            return [_agent_summary(session, item) for item in definitions]

    @app.get("/api/agents/{agent_id}", response_model=AgentDetail, tags=["management"])
    async def get_agent(
        agent_id: str, _: Principal = Depends(require_role("viewer"))
    ) -> dict[str, Any]:
        with database.session() as session:
            definition = session.get(AgentDefinition, agent_id)
            if definition is None or not definition.active:
                raise HTTPException(status_code=404, detail="Agent not found")
            summary = _agent_summary(session, definition)
            descriptor = session.scalar(
                select(DescriptorRecord).where(DescriptorRecord.agent_id == agent_id)
            )
            handlers = list(
                session.scalars(
                    select(Handler).where(Handler.agent_id == agent_id).order_by(Handler.name)
                )
            )
            instances = list(
                session.scalars(
                    select(RuntimeInstance)
                    .where(RuntimeInstance.agent_id == agent_id)
                    .order_by(
                        RuntimeInstance.started_at.desc(),
                        RuntimeInstance.runtime_instance_id.desc(),
                    )
                    .limit(100)
                )
            )
            triggers = list(
                session.scalars(
                    select(Trigger).where(Trigger.agent_id == agent_id).order_by(Trigger.id)
                )
            )
            schedules = list(
                session.scalars(
                    select(Schedule).where(Schedule.agent_id == agent_id).order_by(Schedule.id)
                )
            )
            usage_records = list(
                session.scalars(
                    select(UsageRecord)
                    .where(UsageRecord.agent_id == agent_id)
                    .order_by(UsageRecord.recorded_at.desc(), UsageRecord.id.desc())
                    .limit(100)
                )
            )
            heartbeat = summary["last_heartbeat"]
            heartbeat_age = (
                (utcnow() - ensure_aware(heartbeat)).total_seconds()
                if heartbeat is not None
                else None
            )
            raw_plugins = descriptor.plugins if descriptor else []
            return {
                **summary,
                "manifest": definition.snapshot,
                "descriptor": descriptor.raw if descriptor else None,
                "handlers": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "input_schema": item.input_schema,
                        "output_schema": item.output_schema,
                        "default_timeout_seconds": item.default_timeout_seconds,
                        "max_concurrency": item.max_concurrency,
                        "queue_capacity": item.queue_capacity,
                        "queue_policy": item.queue_policy,
                    }
                    for item in handlers
                ],
                "plugins": [_normalise_plugin(item) for item in raw_plugins],
                "runtime_instances": [serialize_runtime(item) for item in instances],
                "triggers": [
                    {
                        "trigger_id": item.trigger_id,
                        "agent_id": item.agent_id,
                        "type": item.type,
                        "handler": item.handler,
                        "enabled": bool(item.configuration.get("enabled", True)),
                        "cron": item.configuration.get("cron"),
                        "timezone": item.configuration.get("timezone"),
                        "overlap": item.configuration.get("overlap"),
                    }
                    for item in triggers
                ],
                "schedules": [_schedule_view(item) for item in schedules],
                "usage": [serialize_usage(item) for item in usage_records],
                "health": {
                    "status": (
                        "healthy"
                        if summary["actual_state"] == "ready"
                        else (
                            "unhealthy"
                            if summary["actual_state"] in {"failed", "unhealthy", "lost"}
                            else "unknown"
                        )
                    ),
                    "heartbeat_age_seconds": heartbeat_age,
                    "checks": [
                        {
                            "name": str(name),
                            "status": (
                                "healthy"
                                if value in {True, "ok", "healthy", "ready"}
                                else "unhealthy"
                            ),
                            "detail": None if isinstance(value, bool) else str(value),
                        }
                        for name, value in (
                            (
                                (instances[0].runtime_metadata or {}).get("health_checks", {}) or {}
                            ).items()
                            if instances
                            else []
                        )
                    ],
                },
                "log_url": next((item.log_url for item in instances if item.log_url), None),
                "trace_url": next((item.trace_url for item in instances if item.trace_url), None),
            }

    @app.get(
        "/api/agents/{agent_id}/runtime-instances",
        response_model=list[RuntimeView],
        tags=["management"],
    )
    async def list_runtime_instances(
        agent_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(
            default=0,
            ge=0,
            le=resolved.events.max_runtime_instances_per_agent,
        ),
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(
                    select(RuntimeInstance)
                    .where(RuntimeInstance.agent_id == agent_id)
                    .order_by(
                        RuntimeInstance.started_at.desc(),
                        RuntimeInstance.runtime_instance_id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [serialize_runtime(item) for item in items]

    @app.get(
        "/api/agents/{agent_id}/handlers",
        response_model=list[HandlerView],
        tags=["management"],
    )
    async def list_handlers(
        agent_id: str, _: Principal = Depends(require_role("viewer"))
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(
                    select(Handler).where(Handler.agent_id == agent_id).order_by(Handler.name)
                )
            )
            return [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                    "output_schema": item.output_schema,
                    "default_timeout_seconds": item.default_timeout_seconds,
                    "max_concurrency": item.max_concurrency,
                    "queue_capacity": item.queue_capacity,
                    "queue_policy": item.queue_policy,
                }
                for item in items
            ]

    async def _lifecycle_action(
        agent_id: str,
        action: str,
        principal: Principal,
        request: Request,
    ) -> dict[str, Any]:
        with database.session() as session:
            definition = session.get(AgentDefinition, agent_id)
            if definition is None or not definition.active:
                raise HTTPException(status_code=404, detail="Agent not found")
            if definition.runtime_mode != "resident":
                raise HTTPException(
                    status_code=409,
                    detail="Runtime lifecycle actions are only available for resident Agents",
                )
            if action == "start":
                definition.desired_state = "running"
            elif action == "stop":
                definition.desired_state = "stopped"
            adapter = definition.runtime_adapter
            run_ids = (
                list(
                    session.scalars(
                        select(Run.run_id).where(
                            Run.agent_id == agent_id,
                            Run.status.not_in(TERMINAL_RUN_STATUSES),
                        )
                    )
                )
                if action == "stop"
                else []
            )
        for run_id in run_ids:
            with contextlib.suppress(RunServiceError, RuntimeOperationError):
                await control.runs.cancel(run_id)
        if adapter == "external":
            if action == "restart":
                raise RuntimeOperationError("Workspace cannot restart an externally managed Agent")
            resource_id = None
        elif action == "start":
            runtime = await control.runtime.start_agent(agent_id)
            resource_id = runtime.runtime_instance_id
        elif action == "stop":
            stopped = await control.runtime.stop_agent(agent_id)
            resource_id = stopped[0].runtime_instance_id if stopped else None
        else:
            runtime = await control.runtime.restart_agent(agent_id)
            resource_id = runtime.runtime_instance_id
        with database.session() as session:
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action=f"agent.{action}",
                resource_type="agent",
                resource_id=agent_id,
                remote_address=_remote_address(request),
                redacted_keys=resolved.security.redacted_keys,
            )
        return {"status": "accepted", "resource_id": resource_id}

    @app.post("/api/agents/{agent_id}/start", response_model=OperationResponse, tags=["management"])
    async def start_agent(
        agent_id: str,
        request: Request,
        principal: Principal = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        return await _lifecycle_action(agent_id, "start", principal, request)

    @app.post("/api/agents/{agent_id}/stop", response_model=OperationResponse, tags=["management"])
    async def stop_agent(
        agent_id: str,
        request: Request,
        principal: Principal = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        return await _lifecycle_action(agent_id, "stop", principal, request)

    @app.post(
        "/api/agents/{agent_id}/restart", response_model=OperationResponse, tags=["management"]
    )
    async def restart_agent(
        agent_id: str,
        request: Request,
        principal: Principal = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        return await _lifecycle_action(agent_id, "restart", principal, request)

    @app.post(
        "/api/agents/{agent_id}/runs",
        response_model=RunView,
        status_code=202,
        tags=["management"],
    )
    async def create_run(
        agent_id: str,
        body: RunCreateRequest,
        request: Request,
        principal: Principal = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        run, _ = control.runs.create(
            agent_id=agent_id,
            handler=body.handler,
            source="on_demand",
            input_value=body.input,
            parent_run_id=body.parent_run_id,
            correlation_id=body.correlation_id,
            timeout_seconds=body.timeout_seconds,
        )
        with database.session() as session:
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action="run.create",
                resource_type="run",
                resource_id=run.run_id,
                remote_address=_remote_address(request),
                details={"agent_id": agent_id, "handler": run.handler},
                redacted_keys=resolved.security.redacted_keys,
            )
        await control.events.publish("run", {"run_id": run.run_id, "status": run.status})
        return serialize_run(run)

    @app.get("/api/runs", response_model=list[RunView], tags=["management"])
    async def list_runs(
        agent_id: str | None = None,
        run_status: str | None = Query(default=None, alias="status"),
        correlation_id: str | None = None,
        parent_run_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            query = select(Run)
            if agent_id:
                query = query.where(Run.agent_id == agent_id)
            if run_status:
                query = query.where(Run.status == run_status)
            if correlation_id:
                query = query.where(Run.correlation_id == correlation_id)
            if parent_run_id:
                query = query.where(Run.parent_run_id == parent_run_id)
            items = list(
                session.scalars(
                    query.order_by(Run.created_at.desc(), Run.run_id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [serialize_run(item) for item in items]

    @app.get("/api/runs/{run_id}", response_model=RunView, tags=["management"])
    async def get_run(
        run_id: str, _: Principal = Depends(require_role("viewer"))
    ) -> dict[str, Any]:
        with database.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            usage = list(
                session.scalars(
                    select(UsageRecord)
                    .where(UsageRecord.run_id == run_id)
                    .order_by(UsageRecord.recorded_at.desc(), UsageRecord.id.desc())
                    .limit(100)
                )
            )
            result = serialize_run(run, usage)
            result["children"] = list(
                session.scalars(
                    select(Run.run_id)
                    .where(Run.parent_run_id == run_id)
                    .order_by(Run.created_at.desc(), Run.run_id.desc())
                    .limit(100)
                )
            )
            return result

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView, tags=["management"])
    async def cancel_run(
        run_id: str,
        request: Request,
        principal: Principal = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        run = await control.runs.cancel(run_id)
        with database.session() as session:
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action="run.cancel",
                resource_type="run",
                resource_id=run_id,
                remote_address=_remote_address(request),
                redacted_keys=resolved.security.redacted_keys,
            )
        return serialize_run(run)

    @app.get("/api/runs/{run_id}/events", response_model=list[EventView], tags=["management"])
    async def run_events(
        run_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=resolved.events.max_events_per_run),
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(
                    select(Event)
                    .where(Event.run_id == run_id)
                    .order_by(Event.occurred_at.desc(), Event.event_id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [_event_view(item) for item in items]

    @app.get("/api/runs/{run_id}/usage", response_model=list[UsageView], tags=["management"])
    async def run_usage(
        run_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=resolved.events.max_events_per_run),
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(
                    select(UsageRecord)
                    .where(UsageRecord.run_id == run_id)
                    .order_by(UsageRecord.recorded_at.desc(), UsageRecord.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [serialize_usage(item) for item in items]

    @app.get("/api/schedules", response_model=list[ScheduleView], tags=["management"])
    async def list_schedules(
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(session.scalars(select(Schedule).order_by(Schedule.next_fire_at)))
            return [_schedule_view(item) for item in items]

    @app.get("/api/audit", response_model=list[AuditView], tags=["management"])
    async def list_audit(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(
                    select(AuditRecord)
                    .order_by(AuditRecord.occurred_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [_audit_view(item, resolved.security.redacted_keys) for item in items]

    @app.get(
        "/api/runtime-instances/{runtime_instance_id}/logs",
        response_model=LogsResponse,
        tags=["management"],
    )
    async def runtime_logs(
        runtime_instance_id: str,
        tail: int = Query(default=200, ge=1, le=5000),
        _: Principal = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        with database.session() as session:
            instance = session.get(RuntimeInstance, runtime_instance_id)
            if instance is None:
                raise HTTPException(status_code=404, detail="Runtime Instance not found")
        source, lines = await control.runtime.logs(instance, tail)
        return {
            "runtime_instance_id": runtime_instance_id,
            "source": source,
            "lines": lines,
            "truncated": len(lines) >= tail,
        }

    @app.get("/api/health", response_model=WorkspaceHealthResponse, tags=["management"])
    async def workspace_health(
        _: Principal = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        database_state = "healthy"
        try:
            with database.session() as session:
                session.execute(text("SELECT 1"))
                counts = {
                    state: session.scalar(
                        select(func.count())
                        .select_from(RuntimeInstance)
                        .where(RuntimeInstance.status == state)
                    )
                    or 0
                    for state in ("ready", "unhealthy", "lost")
                }
        except BaseException:
            database_state = "unhealthy"
            counts = {"ready": 0, "unhealthy": 0, "lost": 0}
        scheduler_state = "error" if control.last_error else "running"
        lock_state = "held" if control.instance_lock.acquired else "unavailable"
        overall = (
            "healthy"
            if database_state == "healthy" and scheduler_state == "running" and lock_state == "held"
            else "degraded"
        )
        return {
            "status": overall,
            "database": database_state,
            "instance_lock": lock_state,
            "scheduler": scheduler_state,
            "runtimes": counts,
            "timestamp": utcnow(),
        }

    @app.get("/api/stream", tags=["management"])
    async def stream(
        request: Request,
        principal: Principal = Depends(require_role("viewer")),
    ) -> StreamingResponse:
        try:
            subscription = await control.events.open_subscription(
                principal.subject,
                _remote_address(request) or "unknown",
            )
        except EventStreamLimitExceeded as exc:
            status_code = 503 if exc.boundary == "global" else 429
            raise HTTPException(
                status_code=status_code,
                detail=str(exc),
                headers={"Retry-After": "15"},
            ) from exc

        async def generate() -> AsyncIterator[str]:
            try:
                async for message in control.events.messages(subscription):
                    if await request.is_disconnected():
                        break
                    event_type = message["type"]
                    data = json.dumps(message, separators=(",", ":"), default=str)
                    yield f"event: {event_type}\ndata: {data}\n\n"
            finally:
                await control.events.close_subscription(subscription)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/admin/reload", response_model=ManifestReloadResponse, tags=["admin"])
    async def reload_manifests(
        request: Request,
        principal: Principal = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        report = await control.reload()
        with database.session() as session:
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action="manifest.reload",
                resource_type="workspace",
                resource_id=resolved.workspace.name,
                remote_address=_remote_address(request),
                details=report,
                redacted_keys=resolved.security.redacted_keys,
            )
        return report

    @app.get("/api/admin/tokens", response_model=list[TokenView], tags=["admin"])
    async def list_tokens(_: Principal = Depends(require_role("admin"))) -> list[dict[str, Any]]:
        with database.session() as session:
            items = list(
                session.scalars(select(AgentCredential).order_by(AgentCredential.issued_at.desc()))
            )
            return [
                {
                    "credential_id": item.credential_id,
                    "agent_id": item.agent_id,
                    "description": item.description,
                    "issued_at": item.issued_at,
                    "expires_at": item.expires_at,
                    "last_used_at": item.last_used_at,
                    "revoked_at": item.revoked_at,
                }
                for item in items
            ]

    @app.post("/api/admin/tokens", response_model=TokenIssueResponse, tags=["admin"])
    async def issue_token(
        body: TokenIssueRequest,
        request: Request,
        principal: Principal = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        with database.session() as session:
            if session.get(AgentDefinition, body.agent_id) is None:
                raise HTTPException(status_code=404, detail="Agent not found")
            credential, token = issue_agent_token(
                session, body.agent_id, body.description, body.expires_at
            )
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action="token.issue",
                resource_type="agent_credential",
                resource_id=credential.credential_id,
                remote_address=_remote_address(request),
                details={"agent_id": body.agent_id},
                redacted_keys=resolved.security.redacted_keys,
            )
            return {
                "credential_id": credential.credential_id,
                "agent_id": credential.agent_id,
                "token": token,
                "issued_at": credential.issued_at,
                "expires_at": credential.expires_at,
            }

    @app.delete(
        "/api/admin/tokens/{credential_id}",
        response_model=OperationResponse,
        tags=["admin"],
    )
    async def revoke_token(
        credential_id: str,
        request: Request,
        principal: Principal = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        with database.session() as session:
            credential = session.get(AgentCredential, credential_id)
            if credential is None:
                raise HTTPException(status_code=404, detail="credential not found")
            credential.revoked_at = utcnow()
            audit(
                session,
                actor_type="human",
                actor_id=principal.subject,
                actor_role=_effective_role(principal),
                request_id=request.state.request_id,
                action="token.revoke",
                resource_type="agent_credential",
                resource_id=credential_id,
                remote_address=_remote_address(request),
                details={"agent_id": credential.agent_id},
                redacted_keys=resolved.security.redacted_keys,
            )
        return {"status": "revoked", "resource_id": credential_id}

    @app.post(
        "/api/agent/register",
        response_model=RegistrationResponse,
        status_code=201,
        tags=["agent"],
    )
    async def register_agent(
        body: AgentRegistration,
        request: Request,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        reported_descriptor = body.descriptor.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        claimed_agent_id = reported_descriptor["agent_id"]
        instance_id = str(body.runtime_instance_id)
        supplied_control_url = str(body.control_url) if body.control_url is not None else None
        presented_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        # Authenticate and validate the potentially large descriptor before acquiring
        # SQLite's Agent-scoped admission lock.
        with database.session() as session:
            definition = session.get(AgentDefinition, claimed_agent_id)
            if definition is None or not definition.active:
                raise HTTPException(status_code=404, detail="Agent Definition not found")
            verify_agent_access(session, authorization, claimed_agent_id)
            check_agent_rate_limit(claimed_agent_id)
            persistence_secrets = _resolved_persistence_secrets(
                resolved,
                definition.snapshot,
                {presented_token} if presented_token else None,
            )
            control.telemetry.register_secrets(*persistence_secrets)
            sanitized_descriptor = _sanitize_descriptor(
                reported_descriptor,
                frozenset(resolved.security.redacted_keys),
                frozenset(persistence_secrets),
            )
            try:
                validated_descriptor = AgentDescriptorContract.model_validate(sanitized_descriptor)
            except ValidationError:
                raise HTTPException(
                    status_code=422,
                    detail="Agent descriptor cannot be safely persisted",
                ) from None
            descriptor = validated_descriptor.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            descriptor_size = len(
                json.dumps(
                    descriptor,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if descriptor_size > AGENT_DESCRIPTOR_MAX_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail="Agent descriptor cannot be safely persisted",
                )
            agent_id = descriptor["agent_id"]
            if agent_id != claimed_agent_id:
                raise HTTPException(
                    status_code=422,
                    detail="Agent descriptor identity changed during sanitization",
                )
            fingerprint_control_url = supplied_control_url
            expected_managed_control_url: str | None = None
            if definition.runtime_adapter == "external":
                if definition.runtime_mode == "resident" and not supplied_control_url:
                    raise HTTPException(
                        status_code=422,
                        detail="external resident registration requires control_url",
                    )
                external = (
                    nested(definition.snapshot, "spec", "runtime", "external", default={}) or {}
                )
                declared = (
                    external.get("control_url") or external.get("url") or external.get("endpoint")
                )
                endpoint_required = (
                    definition.runtime_mode == "resident" or supplied_control_url is not None
                )
                if endpoint_required and not isinstance(declared, str):
                    raise HTTPException(
                        status_code=422, detail="external Manifest endpoint is missing"
                    )
                if supplied_control_url:
                    assert isinstance(declared, str)
                    try:
                        supplied_control_url = control.runtime.external.validate_registration_url(
                            declared, supplied_control_url
                        )
                    except RuntimeOperationError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
            elif definition.runtime_adapter == "process":
                if definition.runtime_mode == "resident" and not supplied_control_url:
                    raise HTTPException(
                        status_code=422,
                        detail="resident registration requires control_url",
                    )
                if supplied_control_url:
                    runtime_configuration = (
                        nested(
                            definition.snapshot,
                            "spec",
                            "runtime",
                            "process",
                            default={},
                        )
                        or {}
                    )
                    declared_control_url = runtime_configuration.get("control_url")
                    validator = control.runtime.external.validate_managed_control_url
                    supplied_control_url = validator(supplied_control_url)
                    if definition.runtime_mode == "resident":
                        if not isinstance(declared_control_url, str):
                            raise HTTPException(
                                status_code=422,
                                detail="resident Manifest control_url is missing",
                            )
                        declared_control_url = validator(declared_control_url)
                        if not hmac.compare_digest(declared_control_url, supplied_control_url):
                            raise HTTPException(
                                status_code=422,
                                detail="registration control_url does not match the Manifest",
                            )
                fingerprint_control_url = supplied_control_url
            else:
                provisioned = session.get(RuntimeInstance, instance_id)
                if provisioned is None or provisioned.agent_id != agent_id:
                    raise HTTPException(
                        status_code=409,
                        detail="managed Runtime Instance was not provisioned by Workspace",
                    )
                expected_managed_control_url = provisioned.control_url
                if definition.runtime_mode == "resident":
                    if not isinstance(expected_managed_control_url, str):
                        raise HTTPException(
                            status_code=409,
                            detail="managed Docker Runtime has no derived control URL",
                        )
                    expected_managed_control_url = (
                        control.runtime.external.validate_managed_container_url(
                            expected_managed_control_url
                        )
                    )
                    if supplied_control_url:
                        supplied_control_url = (
                            control.runtime.external.validate_managed_container_url(
                                supplied_control_url
                            )
                        )
                        if not hmac.compare_digest(
                            expected_managed_control_url, supplied_control_url
                        ):
                            raise HTTPException(
                                status_code=422,
                                detail="registration control_url does not match the Runtime",
                            )
                elif supplied_control_url is not None:
                    raise HTTPException(
                        status_code=422,
                        detail="ephemeral Docker registration cannot declare control_url",
                    )
                fingerprint_control_url = expected_managed_control_url
            fingerprint_payload = {
                "descriptor": descriptor,
                "control_url": fingerprint_control_url,
            }
            registration_fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            validated_definition_hash = definition.content_hash

        with database.session() as session:
            definition = lock_agent_definition(session, claimed_agent_id)
            if definition is None or not definition.active:
                raise HTTPException(status_code=404, detail="Agent Definition not found")
            if not hmac.compare_digest(definition.content_hash, validated_definition_hash):
                raise HTTPException(
                    status_code=409,
                    detail="Agent Definition changed during registration; retry the request",
                )
            # Recheck credential and Agent state inside the locked mutation transaction.
            verify_agent_access(session, authorization, claimed_agent_id)
            instance = session.get(RuntimeInstance, instance_id)
            if instance is not None and instance.agent_id != agent_id:
                raise HTTPException(
                    status_code=409, detail="Runtime Instance belongs to another Agent"
                )
            if instance is not None and (
                instance.stopped_at is not None
                or instance.status in {"stopping", "stopped", "failed", "lost"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="stopping or terminal Runtime Instance cannot be registered again",
                )
            if instance is not None and definition.runtime_adapter == "docker":
                current_control_url = instance.control_url
                if current_control_url != expected_managed_control_url:
                    raise HTTPException(
                        status_code=409,
                        detail="managed Docker Runtime endpoint changed during registration",
                    )
            if instance is not None:
                previous_fingerprint = (instance.runtime_metadata or {}).get(
                    "registration_fingerprint"
                )
                if isinstance(previous_fingerprint, str):
                    if not hmac.compare_digest(previous_fingerprint, registration_fingerprint):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Runtime Instance registration does not match "
                                "its first registration"
                            ),
                        )
                    return {
                        "agent_id": agent_id,
                        "runtime_instance_id": instance_id,
                        "status": instance.status,
                    }
            elif definition.runtime_adapter != "external":
                raise HTTPException(
                    status_code=409,
                    detail="managed Runtime Instance was not provisioned by Workspace",
                )

            descriptor_started_at = datetime.fromisoformat(
                str(descriptor["started_at"]).replace("Z", "+00:00")
            )
            registered_at = utcnow()
            if instance is None:
                storage_usage = control.runtime.storage.lock_usage(session, agent_id)
                control.runtime.storage.ensure_active_runtime_capacity(session, agent_id)
                control.runtime.storage.reserve(storage_usage, runtimes=1)
                instance = RuntimeInstance(
                    runtime_instance_id=instance_id,
                    agent_id=agent_id,
                    adapter=definition.runtime_adapter,
                    mode=definition.runtime_mode,
                    status="ready",
                    started_at=descriptor_started_at,
                    ready_at=registered_at,
                    runtime_metadata={"manifest_hash": definition.content_hash},
                )
                session.add(instance)

            record = session.scalar(
                select(DescriptorRecord).where(DescriptorRecord.agent_id == agent_id)
            )
            if record is None:
                record = DescriptorRecord(agent_id=agent_id)
                session.add(record)
            record.application_version = descriptor["application_version"]
            record.sdk_version = descriptor["sdk_version"]
            record.framework = descriptor.get("framework")
            record.build_revision = descriptor.get("build_revision")
            record.plugins = descriptor.get("plugins", [])
            record.capabilities = descriptor.get("capabilities", {})
            record.started_at = descriptor_started_at
            record.reported_at = registered_at
            record.raw = descriptor
            for old_handler in list(
                session.scalars(select(Handler).where(Handler.agent_id == agent_id))
            ):
                session.delete(old_handler)
            session.flush()
            invocation = nested(definition.snapshot, "spec", "invocation", default={}) or {}
            handler_limits = invocation.get("handlers", {}) or {}
            for handler in descriptor.get("handlers", []):
                limits = (
                    handler_limits.get(handler["name"], {})
                    if isinstance(handler_limits, dict)
                    else {}
                )
                session.add(
                    Handler(
                        agent_id=agent_id,
                        name=handler["name"],
                        description=handler.get("description"),
                        input_schema=handler.get("input_schema"),
                        output_schema=handler.get("output_schema"),
                        default_timeout_seconds=handler.get("default_timeout_seconds"),
                        max_concurrency=limits.get("max_concurrency"),
                        queue_capacity=limits.get("queue_capacity"),
                        queue_policy=limits.get("queue_policy"),
                    )
                )

            if definition.runtime_adapter != "docker":
                instance.control_url = supplied_control_url
            requires_readiness_probe = (
                definition.runtime_mode == "resident" and instance.control_url is not None
            )
            registration_status = "starting" if requires_readiness_probe else "ready"
            instance.status = registration_status
            if registration_status == "ready":
                instance.ready_at = instance.ready_at or utcnow()
            instance.last_heartbeat_at = registered_at
            metadata = dict(instance.runtime_metadata or {})
            metadata["registration_fingerprint"] = registration_fingerprint
            metadata["registered_at"] = registered_at.isoformat()
            instance.runtime_metadata = metadata
            audit(
                session,
                actor_type="agent",
                actor_id=agent_id,
                actor_role="agent",
                request_id=request.state.request_id,
                action="agent.register",
                resource_type="runtime_instance",
                resource_id=instance_id,
                redacted_keys=resolved.security.redacted_keys,
            )
        await control.events.publish(
            "runtime",
            {
                "runtime_instance_id": instance_id,
                "agent_id": agent_id,
                "status": registration_status,
            },
        )
        return {
            "agent_id": agent_id,
            "runtime_instance_id": instance_id,
            "status": registration_status,
        }

    @app.post("/api/agent/heartbeat", response_model=OperationResponse, tags=["agent"])
    async def agent_heartbeat(
        body: AgentHeartbeat,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        instance_id = str(body.runtime_instance_id)
        received_at = utcnow()
        # Reject invalid credentials before taking the shared Agent mutation lock.
        with database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            if instance is None:
                raise HTTPException(status_code=404, detail="Runtime Instance not found")
            if body.agent_id != instance.agent_id:
                raise HTTPException(status_code=403, detail="heartbeat Agent scope mismatch")
            verify_agent_access(session, authorization, body.agent_id)
            check_agent_rate_limit(body.agent_id)

        with database.session() as session:
            definition = lock_agent_definition(session, body.agent_id)
            if definition is None or not definition.active:
                raise HTTPException(status_code=404, detail="Agent Definition not found")
            verify_agent_access(session, authorization, body.agent_id)
            instance = session.get(RuntimeInstance, instance_id)
            if instance is None:
                raise HTTPException(status_code=404, detail="Runtime Instance not found")
            if body.agent_id != instance.agent_id:
                raise HTTPException(status_code=403, detail="heartbeat Agent scope mismatch")
            if instance.stopped_at is not None or instance.status in {
                "stopping",
                "stopped",
                "failed",
                "lost",
            }:
                raise HTTPException(
                    status_code=409,
                    detail="stopping or terminal Runtime Instance cannot accept heartbeats",
                )
            if body.status.value not in {"ready", "unhealthy"}:
                raise HTTPException(
                    status_code=422, detail="heartbeat status must be ready or unhealthy"
                )
            managed_probe_pending = (
                instance.status == "starting"
                and instance.mode == "resident"
                and body.status.value == "ready"
            )
            if not managed_probe_pending:
                instance.status = body.status.value
            published_status = instance.status
            instance.last_heartbeat_at = received_at
            agent_id = instance.agent_id
        await control.events.publish(
            "runtime",
            {
                "runtime_instance_id": instance_id,
                "agent_id": agent_id,
                "status": published_status,
            },
        )
        return {"status": "accepted", "resource_id": instance_id}

    @app.post(
        "/api/agent/events/batch",
        response_model=EventBatchResponse,
        status_code=202,
        tags=["agent"],
    )
    async def agent_events(
        body: EventBatch,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        first_agent = body.events[0].agent_id
        with database.session() as session:
            verify_agent_access(session, authorization, first_agent)
            check_agent_rate_limit(first_agent)
        presented_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        events = [
            event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in body.events
        ]
        result = control.events_service.ingest(
            first_agent,
            events,
            redacted_values={presented_token} if presented_token else None,
        )
        duplicate_ids = set(result["duplicates"])
        published_ids: set[str] = set()
        for event in events:
            event_id = str(event.get("event_id"))
            if event_id in duplicate_ids or event_id in published_ids:
                continue
            published_ids.add(event_id)
            await control.events.publish(
                "event",
                {
                    "event_id": event.get("event_id"),
                    "type": event.get("type"),
                    "agent_id": event.get("agent_id"),
                    "run_id": event.get("run_id"),
                },
            )
        return result

    @app.post(
        "/api/agent/runs/begin",
        response_model=AgentRunAssignment,
        status_code=201,
        tags=["agent"],
    )
    async def agent_run_begin(
        body: AgentRunBegin,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        agent_id = body.agent_id
        with database.session() as session:
            verify_agent_access(session, authorization, agent_id)
            check_agent_rate_limit(agent_id)
        presented_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        run, was_created = control.runs.create(
            agent_id=agent_id,
            handler=body.handler,
            source=body.source.value,
            input_value=body.input,
            parent_run_id=str(body.parent_run_id) if body.parent_run_id else None,
            correlation_id=str(body.correlation_id),
            trace_id=body.trace_id,
            timeout_seconds=body.timeout_seconds,
            idempotency_key=body.idempotency_key,
            run_id=str(body.run_id),
            runtime_instance_id=str(body.runtime_instance_id),
            start_running=True,
            require_runtime_assignment=True,
            redacted_values={presented_token} if presented_token else None,
        )
        if was_created:
            await control.events.publish("run", {"run_id": run.run_id, "status": run.status})
        return _run_assignment(run)

    @app.get(
        "/api/agent/runs/{run_id}/input",
        response_model=AgentRunAssignment,
        tags=["agent"],
    )
    async def agent_run_input(
        run_id: str,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        with database.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            verify_agent_access(session, authorization, run.agent_id)
            check_agent_rate_limit(run.agent_id)
            return _run_assignment(run)

    @app.post("/api/agent/runs/{run_id}/ack", response_model=RunView, tags=["agent"])
    async def agent_run_ack(
        run_id: str,
        body: AgentRunAcknowledgement,
        authorization: str = Depends(agent_authorization),
    ) -> dict[str, Any]:
        with database.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            verify_agent_access(session, authorization, run.agent_id)
            check_agent_rate_limit(run.agent_id)
            if run.status in TERMINAL_RUN_STATUSES:
                raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
            runtime_instance_id = str(body.runtime_instance_id)
            if run.runtime_instance_id != runtime_instance_id:
                raise HTTPException(
                    status_code=409,
                    detail="ack Runtime Instance does not match the Run assignment",
                )
            instance = session.get(RuntimeInstance, runtime_instance_id)
            if instance is None or instance.agent_id != run.agent_id:
                raise HTTPException(status_code=422, detail="invalid Runtime Instance")
            if run.status == "created":
                _transition(run, "queued")
            if run.status == "queued":
                _transition(run, "dispatching")
            _transition(run, "running")
            result = serialize_run(run)
        await control.events.publish("run", {"run_id": run_id, "status": result["status"]})
        return result

    @app.post(
        "/hooks/{agent_id}/{trigger_id}", response_model=RunView, status_code=202, tags=["webhook"]
    )
    async def webhook(agent_id: str, trigger_id: str, request: Request) -> dict[str, Any]:
        remote = _remote_address(request) or "unknown"
        with database.session() as session:
            trigger = session.scalar(
                select(Trigger).where(
                    Trigger.agent_id == agent_id,
                    Trigger.trigger_id == trigger_id,
                    Trigger.type == "webhook",
                )
            )
            if trigger is None:
                raise HTTPException(status_code=404, detail="webhook trigger not found")
            configuration = trigger.configuration
        rate_limiter.check(
            f"webhook:{agent_id}:{trigger_id}:{remote}",
            int(configuration.get("rate_limit_per_minute", resolved.security.webhook_rate_limit)),
        )
        maximum = min(
            int(configuration.get("max_request_bytes", resolved.events.max_input_bytes)),
            resolved.events.max_input_bytes,
        )
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > maximum:
            raise HTTPException(status_code=413, detail="webhook payload too large")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > maximum:
                raise HTTPException(status_code=413, detail="webhook payload too large")
        raw_body = bytes(body)
        shared_reference = configuration.get("shared_secret_ref")
        hmac_reference = configuration.get("hmac_secret_ref")
        signature_header = configuration.get("hmac_header", "X-Kitsune-Signature")
        signature = request.headers.get(signature_header)
        shared = request.headers.get("X-Kitsune-Webhook-Secret")
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            shared = authorization.removeprefix("Bearer ")
        authenticated = False
        if signature:
            reference = hmac_reference or shared_reference
            if not isinstance(reference, str):
                raise HTTPException(status_code=401, detail="webhook signing is not configured")
            try:
                secret = resolve_secret_reference(reference)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=503, detail="webhook secret unavailable") from exc
            supplied = signature.removeprefix("sha256=")
            expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            authenticated = hmac.compare_digest(supplied, expected)
        elif shared:
            if isinstance(shared_reference, str):
                try:
                    secret = resolve_secret_reference(shared_reference)
                except (OSError, ValueError) as exc:
                    raise HTTPException(
                        status_code=503, detail="webhook secret unavailable"
                    ) from exc
                authenticated = hmac.compare_digest(shared, secret)
        if not authenticated:
            raise HTTPException(
                status_code=401, detail="invalid webhook signature or shared secret"
            )
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            try:
                payload: Any = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="invalid JSON webhook payload") from exc
        else:
            payload = raw_body.decode("utf-8", errors="replace")
        idempotency = request.headers.get(
            configuration.get("idempotency_header", "Idempotency-Key")
        )
        scoped_key = f"webhook:{trigger_id}:{idempotency}" if idempotency else None
        with control.telemetry.span(
            "kitsune.webhook.dispatch", agent_id=agent_id, trigger_id=trigger_id
        ):
            run, was_created = control.runs.create(
                agent_id=agent_id,
                handler=trigger.handler,
                source="webhook",
                trigger_id=trigger_id,
                input_value=payload,
                idempotency_key=scoped_key,
            )
        if was_created:
            with database.session() as session:
                audit(
                    session,
                    actor_type="webhook",
                    actor_id=f"{agent_id}/{trigger_id}",
                    actor_role="webhook",
                    request_id=request.state.request_id,
                    action="webhook.dispatch",
                    resource_type="run",
                    resource_id=run.run_id,
                    remote_address=remote,
                    details={"idempotency_key_present": idempotency is not None},
                    redacted_keys=resolved.security.redacted_keys,
                )
            await control.events.publish("run", {"run_id": run.run_id, "status": run.status})
        return serialize_run(run)

    static_directory = resolved.workspace.static_directory
    if static_directory is None:
        static_directory = Path(__file__).resolve().parents[4] / "apps" / "workspace-web" / "dist"
    if static_directory.is_dir():
        assets = static_directory / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="workspace-assets")

        @app.get("/{spa_path:path}", include_in_schema=False)
        async def spa(spa_path: str) -> Response:
            if spa_path.startswith(("api/", "hooks/", "healthz", "readyz")):
                raise HTTPException(status_code=404)
            candidate = (static_directory / spa_path).resolve()
            if candidate.is_relative_to(static_directory.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_directory / "index.html")

    _install_openapi_contract(app, session_cookie_name=resolved.auth.session_cookie_name)
    return app
