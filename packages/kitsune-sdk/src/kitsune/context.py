"""Run correlation, cancellation, lineage, usage, and child-Run context."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from kitsune_contracts import (
    EventSeverity,
    KitsuneEvent,
    RunError,
    RunOutcome,
    RunSource,
    RunStatus,
    UsageRecord,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from .logging import contextual_logger
from .telemetry import current_trace_id, set_span_attributes
from .workspace import _WorkspaceClientProtocol

EventEmitter = Callable[[KitsuneEvent, bool], Awaitable[None]]
ChildGuard = Callable[["RunContext"], Awaitable[None]]
ModelGuard = Callable[["RunContext", bool], Awaitable[None]]
UsageObserver = Callable[["RunContext", UsageRecord], Awaitable[None]]
UsageReservationRelease = Callable[[], None]
UsageEventReserver = Callable[["RunContext"], Awaitable[UsageReservationRelease | None]]
RunEventCapacityReserver = Callable[["RunContext"], Awaitable[None]]
RunEventCapacityReleaser = Callable[[UUID], None]
ContextInitializer = Callable[["RunContext"], None]
ChildStarted = Callable[["RunContext"], Awaitable[None]]
ChildFinished = Callable[["RunContext", RunOutcome], Awaitable[None]]


def _record_safe_exception(span: Span, exception: BaseException) -> None:
    span.add_event("exception", {"exception.type": type(exception).__name__})
    span.set_status(Status(StatusCode.ERROR))


class RunCancelled(asyncio.CancelledError):
    """Typed cooperative cancellation raised by :class:`RunCancelScope`."""


class ModelCallAdmission:
    """Held Usage Event capacity that a failed provider request must release."""

    def __init__(self, release: UsageReservationRelease | None) -> None:
        self._release = release
        self._released = False

    def release(self) -> None:
        """Release capacity when the admitted provider request produces no Usage."""

        if self._released:
            return
        self._released = True
        if self._release is not None:
            self._release()


class RunCancelScope:
    """Cooperative cancellation state inherited by child Run Contexts."""

    def __init__(self, *, parent: RunCancelScope | None = None) -> None:
        self._event = asyncio.Event()
        self._parent = parent

    @property
    def cancelled(self) -> bool:
        """Return whether this scope or an ancestor has been cancelled."""

        return self._event.is_set() or bool(self._parent and self._parent.cancelled)

    def cancel(self) -> None:
        """Mark this scope cancelled without mutating its parent."""

        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelled` when cancellation was requested."""

        if self.cancelled:
            raise RunCancelled("Kitsune Run cancellation requested")

    async def wait(self) -> None:
        """Wait until this scope or an ancestor becomes cancelled."""

        if self.cancelled:
            return
        tasks: list[asyncio.Task[Any]] = [asyncio.create_task(self._event.wait())]
        if self._parent is not None:
            tasks.append(asyncio.create_task(self._parent.wait()))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)


class RunContext:
    """Immutable Run identity plus bounded execution and extension capabilities."""

    def __init__(
        self,
        *,
        agent_id: str,
        runtime_instance_id: UUID,
        run_id: UUID,
        parent_run_id: UUID | None,
        correlation_id: UUID,
        source: RunSource,
        started_at: datetime,
        deadline: datetime | None,
        cancel_scope: RunCancelScope,
        logger: logging.LoggerAdapter[logging.Logger],
        tracer: Tracer,
        workspace_client: _WorkspaceClientProtocol | None,
        metadata: Mapping[str, Any] | None = None,
        event_emitter: EventEmitter | None = None,
        child_guards: tuple[ChildGuard, ...] = (),
        model_guards: tuple[ModelGuard, ...] = (),
        usage_observers: tuple[UsageObserver, ...] = (),
        usage_event_reserver: UsageEventReserver | None = None,
        run_event_capacity_reserver: RunEventCapacityReserver | None = None,
        run_event_capacity_releaser: RunEventCapacityReleaser | None = None,
        context_initializer: ContextInitializer | None = None,
        child_started: ChildStarted | None = None,
        child_finished: ChildFinished | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.runtime_instance_id = runtime_instance_id
        self.run_id = run_id
        self.parent_run_id = parent_run_id
        self.correlation_id = correlation_id
        self.source = source
        self.started_at = started_at
        self.deadline = deadline
        self.cancel_scope = cancel_scope
        self.logger = logger
        self.tracer = tracer
        self.workspace_client = workspace_client
        self.metadata = MappingProxyType(dict(metadata or {}))
        self._event_emitter = event_emitter
        self._child_guards = child_guards
        self._model_guards = model_guards
        self._usage_observers = usage_observers
        self._usage_event_reserver = usage_event_reserver
        self._run_event_capacity_reserver = run_event_capacity_reserver
        self._run_event_capacity_releaser = run_event_capacity_releaser
        self._context_initializer = context_initializer
        self._child_started = child_started
        self._child_finished = child_finished
        self._extensions: dict[str, Any] = {}
        self._usage: list[UsageRecord] = []

    @property
    def trace_id(self) -> str | None:
        """Return the currently active OpenTelemetry trace ID."""

        return current_trace_id()

    @property
    def usage(self) -> tuple[UsageRecord, ...]:
        """Return exact Usage Records observed during this Run."""

        return tuple(self._usage)

    @property
    def extensions(self) -> Mapping[str, Any]:
        """Return the named public Context extensions installed before startup."""

        return MappingProxyType(self._extensions)

    @property
    def soft_limit_reached(self) -> bool:
        """Return whether any installed Context extension reports a soft limit."""

        return any(
            bool(getattr(extension, "soft_limit_reached", False))
            for extension in self._extensions.values()
        )

    def extension(self, name: str) -> Any:
        """Return a named Context extension or raise ``KeyError`` when absent."""

        return self._extensions[name]

    def install_extension(self, name: str, extension: Any) -> None:
        """Install one preconfigured extension while a Context is being constructed."""

        if name in self._extensions:
            raise ValueError(f"Run Context extension {name!r} is already installed")
        self._extensions[name] = extension

    def raise_if_cancelled(self) -> None:
        """Raise when cancellation was requested or the wall-clock deadline passed."""

        self.cancel_scope.raise_if_cancelled()
        if self.deadline is not None and datetime.now(UTC) >= self.deadline:
            raise TimeoutError("Kitsune Run deadline exceeded")

    async def check_model_call(self, *, finalization: bool = False) -> ModelCallAdmission:
        """Reserve Usage durability, run guards, and return failed-request cleanup."""

        self.raise_if_cancelled()
        release_reservation = (
            await self._usage_event_reserver(self)
            if self._usage_event_reserver is not None
            else None
        )
        admission = ModelCallAdmission(release_reservation)
        try:
            for guard in self._model_guards:
                await guard(self, finalization)
        except BaseException:
            admission.release()
            raise
        return admission

    async def record_usage(self, usage: UsageRecord) -> None:
        """Attach exact model usage and notify registered observers."""

        self._usage.append(usage)
        for observer in self._usage_observers:
            await observer(self, usage)

    async def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> KitsuneEvent:
        """Emit an Agent-specific or standard namespaced event for this Run."""

        event = KitsuneEvent(
            type=event_type,
            occurred_at=datetime.now(UTC),
            agent_id=self.agent_id,
            runtime_instance_id=self.runtime_instance_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            correlation_id=self.correlation_id,
            trace_id=self.trace_id,
            severity=severity,
            payload=dict(payload or {}),
        )
        if self._event_emitter is not None:
            await self._event_emitter(event, True)
        else:
            self.logger.info(event_type, extra={"event": event_type, "details": event.payload})
        return event

    @asynccontextmanager
    async def child_run(
        self,
        *,
        name: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[RunContext]:
        """Create a child Run that inherits correlation, deadline, trace, and cancellation."""

        self.raise_if_cancelled()
        child_id = uuid4()
        started_at = datetime.now(UTC)
        child_metadata = {**self.metadata, **dict(metadata or {}), "name": name}
        with self.tracer.start_as_current_span(
            "kitsune.child_run",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            set_span_attributes(
                span,
                {
                    "kitsune.agent.id": self.agent_id,
                    "kitsune.run.id": str(child_id),
                    "kitsune.run.parent_id": str(self.run_id),
                    "kitsune.run.correlation_id": str(self.correlation_id),
                    "kitsune.child.name": name,
                },
            )
            child = RunContext(
                agent_id=self.agent_id,
                runtime_instance_id=self.runtime_instance_id,
                run_id=child_id,
                parent_run_id=self.run_id,
                correlation_id=self.correlation_id,
                source=RunSource.CHILD,
                started_at=started_at,
                deadline=self.deadline,
                cancel_scope=RunCancelScope(parent=self.cancel_scope),
                logger=contextual_logger(
                    self.logger.logger,
                    agent_id=self.agent_id,
                    runtime_instance_id=str(self.runtime_instance_id),
                    run_id=str(child_id),
                    parent_run_id=str(self.run_id),
                    correlation_id=str(self.correlation_id),
                    trace_id=current_trace_id(),
                ),
                tracer=self.tracer,
                workspace_client=self.workspace_client,
                metadata=child_metadata,
                event_emitter=self._event_emitter,
                child_guards=self._child_guards,
                model_guards=self._model_guards,
                usage_observers=self._usage_observers,
                usage_event_reserver=self._usage_event_reserver,
                run_event_capacity_reserver=self._run_event_capacity_reserver,
                run_event_capacity_releaser=self._run_event_capacity_releaser,
                context_initializer=self._context_initializer,
                child_started=self._child_started,
                child_finished=self._child_finished,
            )
            if self._context_initializer is not None:
                self._context_initializer(child)
            try:
                if self._run_event_capacity_reserver is not None:
                    await self._run_event_capacity_reserver(child)
                for guard in self._child_guards:
                    await guard(self)
                if self._child_started is not None:
                    await self._child_started(child)
            except BaseException as exc:
                _record_safe_exception(span, exc)
                if self._run_event_capacity_releaser is not None:
                    self._run_event_capacity_releaser(child.run_id)
                raise
            status: RunStatus = RunStatus.SUCCEEDED
            error: RunError | None = None
            try:
                yield child
            except asyncio.CancelledError:
                status = RunStatus.CANCELLED
                raise
            except TimeoutError as exc:
                status = RunStatus.TIMED_OUT
                error = RunError(type=type(exc).__name__, message="Run deadline exceeded")
                _record_safe_exception(span, exc)
                raise
            except Exception as exc:
                status = RunStatus.FAILED
                error = RunError(type=type(exc).__name__, message="Child Run failed")
                _record_safe_exception(span, exc)
                raise
            finally:
                outcome = RunOutcome(
                    status=status,
                    error=error if status in {RunStatus.FAILED, RunStatus.TIMED_OUT} else None,
                    usage=list(child.usage),
                    ended_at=datetime.now(UTC),
                )
                if self._child_finished is not None:
                    await self._child_finished(child, outcome)
