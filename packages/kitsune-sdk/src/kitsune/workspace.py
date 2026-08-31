"""HTTP client for Agent-facing Kitsune Workspace endpoints."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from kitsune_contracts import (
    AgentHeartbeat,
    AgentRegistration,
    AgentRunAcknowledgement,
    AgentRunAssignment,
    AgentRunBegin,
    EventBatch,
    KitsuneEvent,
    RunSource,
)
from pydantic import TypeAdapter

from .outbox import NonRetryableEventDeliveryError, RetryableEventDeliveryError
from .settings import validate_workspace_transport

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, Any])


class _WorkspaceClientProtocol(Protocol):
    """Workspace operations used by the SDK application lifecycle."""

    async def register(self, registration: AgentRegistration) -> dict[str, Any]: ...

    async def heartbeat(self, heartbeat: AgentHeartbeat) -> None: ...

    async def send_events(self, events: Sequence[KitsuneEvent]) -> None: ...

    async def begin_run(
        self,
        *,
        run_id: UUID,
        agent_id: str,
        runtime_instance_id: UUID,
        handler: str,
        source: Literal[RunSource.SELF, RunSource.CHILD],
        parent_run_id: UUID | None,
        correlation_id: UUID,
        trace_id: str | None = None,
        input_data: Any = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_run_assignment(self, run_id: UUID) -> AgentRunAssignment: ...

    async def acknowledge_run(self, run_id: UUID, *, runtime_instance_id: UUID) -> None: ...

    async def close(self) -> None: ...


class WorkspaceClient:
    """Authenticated asynchronous client for the Agent-facing Workspace API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10,
        max_event_batch_bytes: int = 1_114_112,
        allow_insecure_workspace: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_event_batch_bytes <= 0:
            raise ValueError("max_event_batch_bytes must be positive")
        if not token.strip():
            raise ValueError("token must not be empty")
        validate_workspace_transport(base_url, allow_insecure=allow_insecure_workspace)
        self._max_event_batch_bytes = max_event_batch_bytes
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def register(self, registration: AgentRegistration) -> dict[str, Any]:
        """Register a Runtime Instance and its reported Agent Descriptor."""

        response = await self._client.post(
            "/api/agent/register", json=registration.model_dump(mode="json")
        )
        response.raise_for_status()
        return _json_object(response)

    async def heartbeat(self, heartbeat: AgentHeartbeat) -> None:
        """Send a best-effort Runtime Instance liveness report."""

        response = await self._client.post(
            "/api/agent/heartbeat", json=heartbeat.model_dump(mode="json")
        )
        response.raise_for_status()

    async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
        """Deliver one event batch; callers retain events until this returns."""

        for batch in _split_event_batches(events, self._max_event_batch_bytes):
            response = await self._client.post(
                "/api/agent/events/batch",
                content=batch.model_dump_json(),
                headers={"Content-Type": "application/json"},
            )
            if response.status_code in {400, 413, 422}:
                raise NonRetryableEventDeliveryError(
                    status_code=response.status_code,
                    detail=response.text,
                )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    retry_after_seconds = min(3_600, max(1, int(retry_after)))
                except ValueError:
                    retry_after_seconds = 1
                raise RetryableEventDeliveryError(
                    status_code=response.status_code,
                    detail=response.text,
                    retry_after_seconds=retry_after_seconds,
                )
            response.raise_for_status()

    async def begin_run(
        self,
        *,
        run_id: UUID,
        agent_id: str,
        runtime_instance_id: UUID,
        handler: str,
        source: Literal[RunSource.SELF, RunSource.CHILD],
        parent_run_id: UUID | None,
        correlation_id: UUID,
        trace_id: str | None = None,
        input_data: Any = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist one self Handler Run or a child operation before local execution."""

        request = AgentRunBegin(
            run_id=run_id,
            agent_id=agent_id,
            runtime_instance_id=runtime_instance_id,
            handler=handler,
            source=source,
            parent_run_id=parent_run_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            input=input_data,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )
        response = await self._client.post(
            "/api/agent/runs/begin",
            headers={"X-Kitsune-Agent-ID": request.agent_id},
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return _json_object(response)

    async def get_run_input(self, run_id: UUID) -> Any:
        """Fetch the immutable input assigned to an ephemeral Run."""

        assignment = await self.get_run_assignment(run_id)
        return assignment.input

    async def get_run_assignment(self, run_id: UUID) -> AgentRunAssignment:
        """Fetch input and lineage metadata assigned to an ephemeral Run."""

        response = await self._client.get(f"/api/agent/runs/{run_id}/input")
        response.raise_for_status()
        return AgentRunAssignment.model_validate(response.json())

    async def acknowledge_run(self, run_id: UUID, *, runtime_instance_id: UUID) -> None:
        """Acknowledge that an ephemeral process accepted its assigned Run."""

        acknowledgement = AgentRunAcknowledgement(runtime_instance_id=runtime_instance_id)
        response = await self._client.post(
            f"/api/agent/runs/{run_id}/ack",
            json=acknowledgement.model_dump(mode="json"),
        )
        response.raise_for_status()

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self._client.aclose()


def _json_object(response: httpx.Response) -> dict[str, Any]:
    return _JSON_OBJECT_ADAPTER.validate_python(response.json())


def _split_event_batches(
    events: Sequence[KitsuneEvent],
    maximum_bytes: int,
) -> list[EventBatch]:
    batches: list[EventBatch] = []
    current: list[KitsuneEvent] = []
    for event in events:
        candidate = EventBatch(events=[*current, event])
        if current and len(candidate.model_dump_json().encode("utf-8")) > maximum_bytes:
            batches.append(EventBatch(events=current))
            current = [event]
        else:
            current.append(event)
    if current:
        batches.append(EventBatch(events=current))
    return batches
