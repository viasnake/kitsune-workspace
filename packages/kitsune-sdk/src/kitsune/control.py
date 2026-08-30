"""Resident Agent Control API exposed under the reserved ``/_kitsune`` prefix."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from kitsune_contracts import AgentRunAssignment
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .app import DuplicateRunError, HandlerNotFoundError, KitsuneApp
from .outbox import OutboxFullError


class AcceptedRun(BaseModel):
    """Immediate acknowledgement for an asynchronously executing Run."""

    run_id: UUID
    status: str = "accepted"


class _ControlRequestBodyLimitMiddleware:
    """Bound declared and streamed request bodies outside FastAPI routing."""

    def __init__(self, app: ASGIApp, *, maximum_bytes: int) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, raw_value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_bytes = int(raw_value)
            except ValueError:
                continue
            if declared_bytes > self.maximum_bytes:
                await self._reject(scope, receive, send)
                return

        received_bytes = 0
        buffered_messages: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.maximum_bytes:
                    await self._reject(scope, receive, send)
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
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Control API request body too large"},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
        await response(scope, receive, send)


def _canonical_loopback_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    normalized = host.casefold()
    try:
        normalized = ipaddress.ip_address(host).compressed
    except ValueError:
        if normalized != "localhost":
            return None
    return parsed.scheme, normalized, port


def _loopback_host_header(value: str, *, expected_port: int, scheme: str) -> bool:
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


class _LoopbackControlBoundaryMiddleware:
    """Protect tokenless loopback Control APIs from DNS rebinding and browser requests."""

    def __init__(self, app: ASGIApp, *, trusted_origin: str, bind_port: int) -> None:
        self.app = app
        self.bind_port = bind_port
        self.trusted_origin = _canonical_loopback_origin(trusted_origin)
        if self.trusted_origin is None:
            raise ValueError("tokenless Control API requires a loopback trusted origin")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [value.decode("latin-1") for name, value in headers if name.lower() == b"host"]
        if len(hosts) != 1 or not _loopback_host_header(
            hosts[0], expected_port=self.bind_port, scheme=scope.get("scheme", "http")
        ):
            await JSONResponse(status_code=400, content={"detail": "invalid Host header"})(
                scope, receive, send
            )
            return
        origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
        if origins and (
            len(origins) != 1 or _canonical_loopback_origin(origins[0]) != self.trusted_origin
        ):
            await JSONResponse(status_code=403, content={"detail": "cross-origin request denied"})(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def create_control_api(
    application: KitsuneApp,
    *,
    bind_host: str | None = None,
    bind_port: int | None = None,
) -> FastAPI:
    """Build the resident Control API with authenticated mutation endpoints."""

    expected_token = (
        application.settings.agent_token.get_secret_value()
        if application.settings.agent_token is not None
        else None
    )
    effective_bind_host = bind_host or application.settings.bind_host
    effective_bind_port = bind_port or application.settings.bind_port
    if expected_token is not None and not expected_token.strip():
        raise ValueError("KITSUNE_AGENT_TOKEN must be non-empty for the Control API")
    if expected_token is None and not _is_loopback_bind(effective_bind_host):
        raise ValueError("KITSUNE_AGENT_TOKEN is required for a non-loopback Control API bind")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        await application.startup()
        try:
            yield
        finally:
            await application.shutdown()

    api = FastAPI(title=f"{application.agent_id} Kitsune Control API", lifespan=lifespan)
    api.add_middleware(
        _ControlRequestBodyLimitMiddleware,
        maximum_bytes=application.settings.control_max_request_bytes,
    )
    if expected_token is None:
        origin_host = (
            f"[{effective_bind_host}]" if ":" in effective_bind_host else effective_bind_host
        )
        api.add_middleware(
            _LoopbackControlBoundaryMiddleware,
            trusted_origin=f"http://{origin_host}:{effective_bind_port}",
            bind_port=effective_bind_port,
        )

    @api.get("/_kitsune/manifest")
    async def manifest(  # pyright: ignore[reportUnusedFunction]
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        _require_control_bearer(authorization, expected_token)
        return application.sanitized_descriptor()

    @api.get("/_kitsune/healthz")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok" if application.started else "starting"}

    @api.get("/_kitsune/readyz")
    async def readiness() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        code = status.HTTP_200_OK if application.ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(
            {"status": "ready" if application.ready else "not_ready"}, status_code=code
        )

    @api.post(
        "/_kitsune/runs",
        response_model=AcceptedRun,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        request: AgentRunAssignment,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> AcceptedRun:
        _require_control_bearer(authorization, expected_token)
        if not application.ready:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Agent is not ready")
        if request.agent_id != application.agent_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Run assignment targets a different Agent",
            )
        registration = application.handlers.get(request.handler)
        if registration is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Handler is not registered")
        try:
            registration.input_model.model_validate(request.input)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Run input validation failed",
            ) from exc
        try:
            task, created = await application.submit_control_run(
                request.handler,
                request.input,
                run_id=request.run_id,
                source=request.source,
                parent_run_id=request.parent_run_id,
                correlation_id=request.correlation_id,
                deadline=request.deadline,
            )
        except OutboxFullError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Agent Event Outbox cannot durably admit this Run",
            ) from exc
        except DuplicateRunError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "Run is already active") from exc
        if created and task is not None:
            task.add_done_callback(_consume_task_result)
        return AcceptedRun(run_id=request.run_id)

    @api.post("/_kitsune/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> AcceptedRun:
        _require_control_bearer(authorization, expected_token)
        if not await application.cancel(run_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run is not active")
        return AcceptedRun(run_id=run_id, status="cancellation_requested")

    return api


def _require_control_bearer(authorization: str | None, expected_token: str | None) -> None:
    if expected_token is None:
        return
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid Agent Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, credential = (authorization or "").partition(" ")
    provided_token = credential if separator and scheme.casefold() == "bearer" else ""
    if not secrets.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid Agent Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().removeprefix("[").removesuffix("]")
    if normalized.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _consume_task_result(task: asyncio.Task[BaseModel]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, HandlerNotFoundError):
        return
