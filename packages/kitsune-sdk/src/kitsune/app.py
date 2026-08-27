"""Kitsune Agent Application lifecycle, Handler registry, and Run execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from kitsune_contracts import (
    AgentDescriptor,
    AgentHeartbeat,
    AgentRegistration,
    EventSeverity,
    HandlerDescriptor,
    KitsuneEvent,
    PluginDescriptor,
    RunError,
    RunOutcome,
    RunSource,
    RunStatus,
    RuntimeMode,
    UsageRecord,
)
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel

from .context import RunCancelScope, RunContext
from .logging import (
    configure_logging,
    contextual_logger,
    is_sensitive_key,
    redact_sensitive_data,
    redact_sensitive_text,
)
from .outbox import (
    EphemeralDeliveryIncompleteError,
    EventOutbox,
    EventOutboxReservation,
    OutboxDrainResult,
    OutboxFullError,
)
from .plugins import (
    AppBuilder,
    ChildRunGuard,
    ContextExtensionFactory,
    CriticalPluginHookError,
    KitsunePlugin,
    ModelCallGuard,
    PluginHost,
    RunningApp,
    UsageObserver,
)
from .settings import KitsuneSettings
from .telemetry import Telemetry, configure_telemetry, current_trace_id, set_span_attributes
from .workspace import WorkspaceClient, _WorkspaceClientProtocol

SDK_VERSION = "1.0.0"
_TERMINAL_RUN_EVENT_TYPES = frozenset(
    {
        "kitsune.run.succeeded",
        "kitsune.run.failed",
        "kitsune.run.cancelled",
        "kitsune.run.timed_out",
    }
)
_SCHEMA_DEFINITION_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_VALUE_KEYS = frozenset({"const", "default", "enum", "example", "examples"})


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
    """Redact descriptor content while preserving JSON Schema definition names."""

    ordinary = {key: value for key, value in descriptor.items() if key != "handlers"}
    sanitized = redact_sensitive_data(ordinary, redacted_keys, redacted_values)
    if not isinstance(sanitized, dict):  # pragma: no cover - fixed descriptor shape
        raise ValueError("Agent descriptor must be an object")
    sanitized_handlers: list[Any] = []
    for raw_handler in descriptor.get("handlers", []):
        if not isinstance(raw_handler, dict):
            sanitized_handlers.append(raw_handler)
            continue
        ordinary_handler = {
            key: value
            for key, value in raw_handler.items()
            if key not in {"input_schema", "output_schema"}
        }
        sanitized_handler = redact_sensitive_data(ordinary_handler, redacted_keys, redacted_values)
        if not isinstance(sanitized_handler, dict):  # pragma: no cover - fixed shape
            raise ValueError("Agent handler descriptor must be an object")
        for schema_key in ("input_schema", "output_schema"):
            if schema_key in raw_handler:
                sanitized_handler[schema_key] = _sanitize_descriptor_schema(
                    raw_handler[schema_key], redacted_keys, redacted_values
                )
        sanitized_handlers.append(sanitized_handler)
    sanitized["handlers"] = sanitized_handlers
    return sanitized


def _normalized_redaction_identifier(value: str) -> str:
    """Normalize configured keys and environment names for portable matching."""

    return "".join(character for character in value.casefold() if character.isalnum())


def _environment_redacted_values(settings: KitsuneSettings) -> set[str]:
    """Resolve explicit and secret-like environment values without retaining their names."""

    redacted_names = set(settings.redacted_environment_variables)
    redacted_key_fragments = {
        normalized
        for key in settings.redacted_keys
        if (normalized := _normalized_redaction_identifier(key))
    }
    redacted_names.update(
        name
        for name in os.environ
        if any(
            fragment in _normalized_redaction_identifier(name)
            for fragment in redacted_key_fragments
        )
    )
    return {value for name in redacted_names if (value := os.getenv(name))}


type HandlerCallable[InputModelT: BaseModel, OutputModelT: BaseModel] = Callable[
    [RunContext, InputModelT], Awaitable[OutputModelT]
]


def _record_safe_exception(span: Span, exception: BaseException) -> None:
    """Record failure classification without exporting exception-controlled text."""

    span.add_event("exception", {"exception.type": type(exception).__name__})
    span.set_status(Status(StatusCode.ERROR))


class HandlerNotFoundError(LookupError):
    """Raised when a Run names an unregistered Handler."""


class DuplicateRunError(RuntimeError):
    """Raised when an active Run ID is submitted more than once."""


class AppNotRunningError(RuntimeError):
    """Raised when execution is requested outside the application lifecycle."""


@dataclass(frozen=True, slots=True)
class HandlerRegistration[InputModelT: BaseModel, OutputModelT: BaseModel]:
    """One immutable typed Handler registration."""

    name: str
    input_model: type[InputModelT]
    output_model: type[OutputModelT]
    function: HandlerCallable[InputModelT, OutputModelT]
    description: str
    default_timeout_seconds: int

    def descriptor(self) -> HandlerDescriptor:
        """Build the public schema descriptor for this Handler."""

        return HandlerDescriptor(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(mode="validation"),
            output_schema=self.output_model.model_json_schema(mode="serialization"),
            default_timeout_seconds=self.default_timeout_seconds,
        )


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[Any]
    context: RunContext


@dataclass(slots=True)
class _RunOutboxReservation:
    lifecycle: EventOutboxReservation
    usage: list[EventOutboxReservation]
    owner: asyncio.Task[Any] | None
    started_enqueued: bool = False
    terminal_enqueued: bool = False


class _BuilderFacade(AppBuilder):
    def __init__(self, application: KitsuneApp) -> None:
        self._application = application

    @property
    def agent_id(self) -> str:
        return self._application.agent_id

    def add_child_run_guard(self, guard: ChildRunGuard) -> None:
        self._application._child_guards.append(guard)  # pyright: ignore[reportPrivateUsage]

    def add_model_call_guard(self, guard: ModelCallGuard) -> None:
        self._application._model_guards.append(guard)  # pyright: ignore[reportPrivateUsage]

    def add_usage_observer(self, observer: UsageObserver) -> None:
        self._application._usage_observers.append(  # pyright: ignore[reportPrivateUsage]
            observer
        )

    def add_context_extension(self, name: str, factory: ContextExtensionFactory) -> None:
        factories = self._application._extension_factories  # pyright: ignore[reportPrivateUsage]
        if name in factories:
            raise ValueError(f"Run Context extension {name!r} is already registered")
        factories[name] = factory


class _RunningFacade(RunningApp):
    def __init__(self, application: KitsuneApp) -> None:
        self._application = application

    @property
    def agent_id(self) -> str:
        return self._application.agent_id

    async def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        ctx: RunContext | None = None,
    ) -> KitsuneEvent:
        return await self._application.emit(event_type, payload=payload, ctx=ctx)


class KitsuneApp(AbstractAsyncContextManager["KitsuneApp"]):
    """Typed Agent Application that runs with or without Kitsune Workspace."""

    def __init__(
        self,
        *,
        agent_id: str,
        version: str,
        framework: str = "generic",
        build_revision: str | None = None,
        settings: KitsuneSettings | None = None,
        workspace_client: _WorkspaceClientProtocol | None = None,
        outbox: EventOutbox | None = None,
        logger: logging.Logger | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if not version.strip():
            raise ValueError("version must not be empty")
        self.agent_id = agent_id
        self.version = version
        self.framework = framework
        self.build_revision = build_revision
        self.settings = settings or KitsuneSettings()
        service_name = self.settings.service_name or agent_id
        configured_redacted_values = _environment_redacted_values(self.settings)
        if self.settings.agent_token is not None:
            configured_redacted_values.add(self.settings.agent_token.get_secret_value())
        self._redacted_values = frozenset(configured_redacted_values)
        self.logger = logger or configure_logging(
            service=service_name,
            redacted_keys=self.settings.redacted_keys,
            redacted_values=self._redacted_values,
        )
        self.telemetry = telemetry or configure_telemetry(
            service_name=service_name,
            otlp_endpoint=str(self.settings.otlp_endpoint) if self.settings.otlp_endpoint else None,
        )
        self.workspace_client = workspace_client
        if self.workspace_client is None and self.settings.workspace_url is not None:
            assert self.settings.agent_token is not None
            self.workspace_client = WorkspaceClient(
                str(self.settings.workspace_url),
                self.settings.agent_token.get_secret_value(),
                max_event_batch_bytes=self.settings.event_max_batch_bytes,
                allow_insecure_workspace=self.settings.allow_insecure_workspace,
            )
        self.outbox = outbox
        if self.outbox is None and self.workspace_client is not None:
            self.outbox = EventOutbox(
                self.settings.outbox_path,
                capacity=self.settings.outbox_capacity,
                max_bytes=self.settings.outbox_max_bytes,
                max_event_bytes=self.settings.event_max_batch_bytes,
            )
        self.runtime_instance_id = self.settings.runtime_instance_id
        self._handlers: dict[str, HandlerRegistration[Any, Any]] = {}
        self._plugin_host = PluginHost()
        self._builder = _BuilderFacade(self)
        self._running_facade = _RunningFacade(self)
        self._child_guards: list[ChildRunGuard] = []
        self._model_guards: list[ModelCallGuard] = []
        self._usage_observers: list[UsageObserver] = [self._record_usage_metrics]
        self._extension_factories: dict[str, ContextExtensionFactory] = {}
        self._active_runs: dict[UUID, _ActiveRun] = {}
        self._run_outbox_reservations: dict[UUID, _RunOutboxReservation] = {}
        self._run_outbox_reservation_lock = asyncio.Lock()
        self._submitted_runs: dict[UUID, asyncio.Task[BaseModel]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._delivery_stop = asyncio.Event()
        self._heartbeat_stop = asyncio.Event()
        self._started = False
        self._stopping = False
        self._runtime_mode: RuntimeMode | None = None
        self._outbox_metric_initialized = False
        self._started_at = datetime.now(UTC)

    @property
    def started(self) -> bool:
        """Return whether application startup completed successfully."""

        return self._started and not self._stopping

    @property
    def ready(self) -> bool:
        """Return whether the application currently accepts new Runs."""

        return self.started

    @property
    def handlers(self) -> Mapping[str, HandlerRegistration[Any, Any]]:
        """Return a read-only snapshot of registered Handlers."""

        return dict(self._handlers)

    @property
    def descriptor(self) -> AgentDescriptor:
        """Build the Agent Descriptor currently reported to Workspace."""

        return AgentDescriptor(
            agent_id=self.agent_id,
            application_version=self.version,
            sdk_version=SDK_VERSION,
            framework=self.framework,
            build_revision=self.build_revision,
            handlers=[registration.descriptor() for registration in self._handlers.values()],
            plugins=[
                PluginDescriptor(name=metadata.name, version=metadata.version)
                for metadata in self._plugin_host.metadata
            ],
            started_at=self._started_at,
            capabilities={
                "control_api": True,
                "standalone": True,
                "event_outbox": self.outbox is not None,
            },
        )

    def sanitized_descriptor(self) -> dict[str, Any]:
        """Return the public Descriptor with secret-bearing text removed."""

        return _sanitize_descriptor(
            self.descriptor.model_dump(mode="json"),
            self.settings.redacted_keys,
            self._redacted_values,
        )

    def handler[InputModelT: BaseModel, OutputModelT: BaseModel](
        self,
        name: str,
        *,
        input_model: type[InputModelT],
        output_model: type[OutputModelT],
        description: str = "",
        default_timeout_seconds: int = 900,
    ) -> Callable[
        [HandlerCallable[InputModelT, OutputModelT]], HandlerCallable[InputModelT, OutputModelT]
    ]:
        """Register one named asynchronous Handler with Pydantic input and output models."""

        if self._started:
            raise RuntimeError("handlers cannot be registered after application startup")
        if name in self._handlers:
            raise ValueError(f"handler {name!r} is already registered")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")

        def decorator(
            function: HandlerCallable[InputModelT, OutputModelT],
        ) -> HandlerCallable[InputModelT, OutputModelT]:
            if not inspect.iscoroutinefunction(function):
                raise TypeError("Kitsune handlers must be asynchronous functions")
            self._handlers[name] = HandlerRegistration(
                name=name,
                input_model=input_model,
                output_model=output_model,
                function=function,
                description=description or inspect.getdoc(function) or "",
                default_timeout_seconds=default_timeout_seconds,
            )
            return function

        return decorator

    def use(self, plugin: KitsunePlugin) -> KitsuneApp:
        """Configure and register a Plugin before application startup."""

        self._plugin_host.register(plugin, self._builder)
        return self

    async def startup(self, *, mode: RuntimeMode = RuntimeMode.RESIDENT) -> None:
        """Start one resident or ephemeral application lifecycle."""

        if self._started:
            if self._runtime_mode is not mode:
                raise RuntimeError("application is already started in a different Runtime Mode")
            return
        self._runtime_mode = mode
        self._started_at = datetime.now(UTC)
        self._delivery_stop.clear()
        self._heartbeat_stop.clear()
        if self.outbox is not None and not self._outbox_metric_initialized:
            persisted = await self.outbox.size()
            if persisted:
                self.telemetry.event_outbox_size.add(persisted, {"agent_id": self.agent_id})
            self._outbox_metric_initialized = True
        with self.telemetry.tracer.start_as_current_span(
            "kitsune.agent.start",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            set_span_attributes(span, {"kitsune.agent.id": self.agent_id})
            try:
                await self._plugin_host.start(self._running_facade, self._report_plugin_error)
                if self.workspace_client is not None:
                    await self.workspace_client.register(
                        AgentRegistration(
                            descriptor=self.descriptor,
                            runtime_instance_id=self.runtime_instance_id,
                            control_url=self.settings.control_url,
                        )
                    )
                    if self.outbox is not None:
                        self._spawn_background(self._delivery_loop(), name="kitsune-event-delivery")
                    if mode is RuntimeMode.RESIDENT:
                        self._spawn_background(self._heartbeat_loop(), name="kitsune-heartbeat")
                self._started = True
                await self.emit("kitsune.agent.started", payload={"version": self.version})
            except BaseException as exc:
                _record_safe_exception(span, exc)
                self._started = False
                self._runtime_mode = None
                self._delivery_stop.set()
                self._heartbeat_stop.set()
                for task in tuple(self._background_tasks):
                    task.cancel()
                if self._background_tasks:
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
                try:
                    await self._plugin_host.stop(self._running_facade, self._report_plugin_error)
                except BaseException as cleanup_error:
                    exc.add_note(
                        f"Application startup rollback also failed: {type(cleanup_error).__name__}"
                    )
                raise

    async def shutdown(self) -> None:
        """Stop Run admission, drain work, flush events, and release Plugin resources."""

        if not self._started or self._stopping:
            return
        self._stopping = True
        runtime_mode = self._runtime_mode
        shutdown_deadline = monotonic() + self.settings.shutdown_grace_seconds
        errors: list[tuple[str, BaseException]] = []
        try:
            await self.emit("kitsune.agent.stopping")
        except BaseException as exc:
            self._record_shutdown_error(errors, "emit_stopping", exc)
        current_task = asyncio.current_task()
        active_tasks = [
            active.task
            for active in self._active_runs.values()
            if not active.task.done() and active.task is not current_task
        ]
        pending = set(active_tasks)
        if active_tasks:
            try:
                _, pending = await asyncio.wait(
                    active_tasks, timeout=max(0, shutdown_deadline - monotonic())
                )
            except BaseException as exc:
                self._record_shutdown_error(errors, "drain_active_runs", exc)
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    try:
                        await asyncio.gather(*pending, return_exceptions=True)
                    except BaseException as exc:
                        self._record_shutdown_error(errors, "cancel_active_runs", exc)
        try:
            await self._plugin_host.stop(self._running_facade, self._report_plugin_error)
        except BaseException as exc:
            self._record_shutdown_error(errors, "stop_plugins", exc)
        try:
            await self.emit("kitsune.agent.stopped")
        except BaseException as exc:
            self._record_shutdown_error(errors, "emit_stopped", exc)
        self._delivery_stop.set()
        self._heartbeat_stop.set()
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            try:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            except BaseException as exc:
                self._record_shutdown_error(errors, "stop_background_tasks", exc)
        if self.outbox is not None and self.workspace_client is not None:
            try:
                drain = await self.outbox.drain(
                    self.workspace_client.send_events,
                    batch_size=self.settings.event_batch_size,
                    max_batch_bytes=self.settings.event_max_batch_bytes,
                    initial_backoff=self.settings.event_retry_initial_seconds,
                    maximum_backoff=self.settings.event_retry_max_seconds,
                    deadline_monotonic=shutdown_deadline,
                )
                if drain.delivered:
                    self.telemetry.event_outbox_size.add(
                        -drain.delivered, {"agent_id": self.agent_id}
                    )
                if drain.failures:
                    self.telemetry.event_delivery_failures_total.add(
                        drain.failures, {"agent_id": self.agent_id}
                    )
                if drain.remaining:
                    self.logger.error(
                        "Kitsune event outbox was not drained before shutdown deadline",
                        extra={
                            "event": "kitsune.event.outbox_not_drained",
                            "details": {"remaining": drain.remaining},
                        },
                    )
                    if runtime_mode is RuntimeMode.EPHEMERAL:
                        self._record_shutdown_error(
                            errors,
                            "drain_ephemeral_outbox",
                            EphemeralDeliveryIncompleteError(
                                remaining=drain.remaining,
                                failures=drain.failures,
                            ),
                        )
            except BaseException as exc:
                self._record_shutdown_error(errors, "drain_outbox", exc)
        if self.workspace_client is not None:
            try:
                await self.workspace_client.close()
            except BaseException as exc:
                self._record_shutdown_error(errors, "close_workspace_client", exc)
        self._started = False
        self._stopping = False
        self._runtime_mode = None
        try:
            self.telemetry.shutdown()
        except BaseException as exc:
            self._record_shutdown_error(errors, "shutdown_telemetry", exc)
        if errors:
            _, primary = next(
                (item for item in errors if isinstance(item[1], asyncio.CancelledError)),
                next(
                    (item for item in errors if not isinstance(item[1], OutboxFullError)),
                    errors[0],
                ),
            )
            for phase, secondary in errors:
                if secondary is not primary:
                    primary.add_note(
                        f"Additional shutdown failure during {phase}: "
                        f"{type(secondary).__name__}: {secondary}"
                    )
            raise primary

    async def __aenter__(self) -> KitsuneApp:
        """Start the application for an asynchronous context manager."""

        await self.startup()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Gracefully stop the application for an asynchronous context manager."""

        await self.shutdown()

    async def execute(
        self,
        handler: str,
        input_data: Any,
        *,
        run_id: UUID | None = None,
        source: RunSource = RunSource.SELF,
        parent_run_id: UUID | None = None,
        correlation_id: UUID | None = None,
        deadline: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BaseModel:
        """Validate and execute one Handler within a correlated Kitsune Run."""

        return await self._execute(
            handler,
            input_data,
            run_id=run_id,
            source=source,
            parent_run_id=parent_run_id,
            correlation_id=correlation_id,
            deadline=deadline,
            metadata=metadata,
            persist_agent_origin=True,
        )

    async def _execute(
        self,
        handler: str,
        input_data: Any,
        *,
        run_id: UUID | None,
        source: RunSource,
        parent_run_id: UUID | None,
        correlation_id: UUID | None,
        deadline: datetime | None,
        metadata: Mapping[str, Any] | None,
        persist_agent_origin: bool,
    ) -> BaseModel:
        """Execute one Run, optionally persisting an Agent-originated Run first."""

        if not self.started:
            raise AppNotRunningError("Kitsune application is not accepting Runs")
        registration = self._handlers.get(handler)
        if registration is None:
            raise HandlerNotFoundError(handler)
        actual_run_id = run_id or uuid4()
        if actual_run_id in self._active_runs:
            raise DuplicateRunError(str(actual_run_id))
        actual_correlation_id = correlation_id or uuid4()
        now = datetime.now(UTC)
        effective_deadline = deadline or now + timedelta(
            seconds=registration.default_timeout_seconds
        )
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("Run execution requires an asyncio Task")
        started = monotonic()
        with self.telemetry.tracer.start_as_current_span(
            "kitsune.run", record_exception=False, set_status_on_exception=False
        ) as run_span:
            set_span_attributes(
                run_span,
                {
                    "kitsune.agent.id": self.agent_id,
                    "kitsune.run.id": str(actual_run_id),
                    "kitsune.run.parent_id": str(parent_run_id) if parent_run_id else None,
                    "kitsune.run.correlation_id": str(actual_correlation_id),
                    "kitsune.run.source": source.value,
                    "kitsune.handler.name": handler,
                },
            )
            context = self._make_context(
                run_id=actual_run_id,
                parent_run_id=parent_run_id,
                correlation_id=actual_correlation_id,
                source=source,
                started_at=now,
                deadline=effective_deadline,
                metadata=metadata,
            )
            requires_workspace_begin = bool(
                persist_agent_origin
                and self.workspace_client is not None
                and (source is RunSource.SELF or source is RunSource.CHILD)
            )
            run_persisted = not requires_workspace_begin
            await self._reserve_run_event_capacity(context)
            self._active_runs[actual_run_id] = _ActiveRun(current_task, context)
            status: RunStatus = RunStatus.SUCCEEDED
            output: BaseModel | None = None
            error: RunError | None = None
            self.telemetry.active_runs.add(1, {"agent_id": self.agent_id, "handler": handler})
            self.telemetry.runs_total.add(
                1, {"agent_id": self.agent_id, "handler": handler, "source": source.value}
            )
            try:
                if requires_workspace_begin:
                    assert self.workspace_client is not None
                    assert source is RunSource.SELF or source is RunSource.CHILD
                    timeout_seconds = max(
                        1, int((effective_deadline - datetime.now(UTC)).total_seconds())
                    )
                    await self.workspace_client.begin_run(
                        run_id=actual_run_id,
                        agent_id=self.agent_id,
                        runtime_instance_id=self.runtime_instance_id,
                        handler=handler,
                        source=source,
                        parent_run_id=parent_run_id,
                        correlation_id=actual_correlation_id,
                        trace_id=context.trace_id,
                        input_data=_json_value(input_data),
                        timeout_seconds=timeout_seconds,
                    )
                    run_persisted = True
                await self.emit("kitsune.run.started", ctx=context, payload={"handler": handler})
                await self._plugin_host.on_run_started(context, self._report_plugin_error)
                remaining = (effective_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise TimeoutError("Kitsune Run deadline exceeded")
                async with asyncio.timeout(remaining):
                    validated_input = registration.input_model.model_validate(input_data)
                    with self.telemetry.tracer.start_as_current_span(
                        "kitsune.handler",
                        record_exception=False,
                        set_status_on_exception=False,
                    ) as handler_span:
                        set_span_attributes(
                            handler_span,
                            {"kitsune.agent.id": self.agent_id, "kitsune.handler.name": handler},
                        )
                        raw_output = await registration.function(context, validated_input)
                    validated_output = registration.output_model.model_validate(raw_output)
                    output = validated_output
                return validated_output
            except TimeoutError as exc:
                status = RunStatus.TIMED_OUT
                error = RunError(type="TimeoutError", message="Run deadline exceeded")
                _record_safe_exception(run_span, exc)
                raise
            except asyncio.CancelledError:
                status = RunStatus.CANCELLED
                context.cancel_scope.cancel()
                raise
            except Exception as exc:
                status = RunStatus.FAILED
                error = RunError(type=type(exc).__name__, message="Handler execution failed")
                _record_safe_exception(run_span, exc)
                self.telemetry.run_failures_total.add(
                    1, {"agent_id": self.agent_id, "handler": handler}
                )
                raise
            finally:
                ended_at = datetime.now(UTC)
                outcome = RunOutcome(
                    status=status,
                    output=output.model_dump(mode="json") if output is not None else None,
                    error=error if status in {RunStatus.FAILED, RunStatus.TIMED_OUT} else None,
                    usage=list(context.usage),
                    ended_at=ended_at,
                )
                try:
                    if run_persisted:
                        try:
                            await self._plugin_host.on_run_finished(
                                context, outcome, self._report_plugin_error
                            )
                        except CriticalPluginHookError as exc:
                            self._log_plugin_cleanup_failure(exc, run_id=actual_run_id)
                        event_type = f"kitsune.run.{status.value}"
                        event_payload: dict[str, Any] = {"handler": handler}
                        if output is not None:
                            event_payload["output"] = output.model_dump(mode="json")
                        if error is not None:
                            event_payload["error"] = error.model_dump(mode="json")
                        try:
                            await self.emit(event_type, ctx=context, payload=event_payload)
                        except CriticalPluginHookError as exc:
                            self._log_plugin_cleanup_failure(exc, run_id=actual_run_id)
                finally:
                    self._release_run_event_capacity(actual_run_id)
                    self._active_runs.pop(actual_run_id, None)
                    self.telemetry.active_runs.add(
                        -1, {"agent_id": self.agent_id, "handler": handler}
                    )
                    self.telemetry.run_duration_seconds.record(
                        monotonic() - started,
                        {"agent_id": self.agent_id, "handler": handler, "status": status.value},
                    )

    async def cancel(self, run_id: UUID) -> bool:
        """Request cooperative cancellation and cancel the active asyncio Task."""

        active = self._active_runs.get(run_id)
        if active is not None:
            active.context.cancel_scope.cancel()
            active.task.cancel()
            return True
        submitted = self._submitted_runs.get(run_id)
        if submitted is not None:
            submitted.cancel()
            return True
        return False

    async def submit(
        self,
        handler: str,
        input_data: Any,
        **run_options: Any,
    ) -> asyncio.Task[BaseModel]:
        """Reserve terminal Event capacity, then schedule one background Handler Run."""

        if not self.started:
            raise AppNotRunningError("Kitsune application is not accepting Runs")
        if handler not in self._handlers:
            raise HandlerNotFoundError(handler)
        submitted_run_id = cast(UUID | None, run_options.get("run_id")) or uuid4()
        if submitted_run_id in self._active_runs or submitted_run_id in self._submitted_runs:
            raise DuplicateRunError(str(submitted_run_id))
        run_options["run_id"] = submitted_run_id
        if not await self._reserve_run_event_capacity_for_run(submitted_run_id, None):
            raise DuplicateRunError(str(submitted_run_id))
        try:
            task = asyncio.create_task(
                self.execute(handler, input_data, **run_options),
                name=f"kitsune-run-{submitted_run_id}",
            )
        except BaseException:
            self._release_run_event_capacity(submitted_run_id)
            raise
        reservation = self._run_outbox_reservations.get(submitted_run_id)
        if reservation is not None:
            reservation.owner = task
        self._background_tasks.add(task)
        self._submitted_runs[submitted_run_id] = task
        task.add_done_callback(self._background_tasks.discard)

        def release_submission(_: asyncio.Task[BaseModel]) -> None:
            self._submitted_runs.pop(submitted_run_id, None)
            self._release_run_event_capacity(submitted_run_id)

        task.add_done_callback(release_submission)
        return task

    async def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        ctx: RunContext | None = None,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> KitsuneEvent:
        """Create and deliver one immutable Kitsune Event."""

        event = KitsuneEvent(
            type=event_type,
            occurred_at=datetime.now(UTC),
            agent_id=self.agent_id,
            runtime_instance_id=self.runtime_instance_id,
            run_id=ctx.run_id if ctx else None,
            parent_run_id=ctx.parent_run_id if ctx else None,
            correlation_id=ctx.correlation_id if ctx else None,
            trace_id=ctx.trace_id if ctx else current_trace_id(),
            severity=severity,
            payload=dict(payload or {}),
        )
        await self._deliver_event(event, notify_plugins=True)
        return event

    async def run_ephemeral_from_environment(self) -> BaseModel:
        """Fetch and execute the Workspace-assigned Run described by environment variables."""

        run_id_text = _required_environment("KITSUNE_RUN_ID")
        handler = _required_environment("KITSUNE_HANDLER")
        if self.workspace_client is None:
            raise RuntimeError("ephemeral managed execution requires KITSUNE_WORKSPACE_URL")
        run_id = UUID(run_id_text)
        assignment = await self.workspace_client.get_run_assignment(run_id)
        if assignment.run_id != run_id:
            raise RuntimeError("Workspace returned an assignment for a different Run")
        if assignment.agent_id != self.agent_id:
            raise RuntimeError("Workspace returned an assignment for a different Agent")
        if assignment.handler != handler:
            raise RuntimeError("KITSUNE_HANDLER does not match the Workspace assignment")
        input_data = assignment.input
        source = RunSource(os.getenv("KITSUNE_RUN_SOURCE") or assignment.source)
        parent_run_id = _optional_uuid(
            os.getenv("KITSUNE_PARENT_RUN_ID") or assignment.parent_run_id,
            "parent_run_id",
        )
        correlation_id = _optional_uuid(
            os.getenv("KITSUNE_CORRELATION_ID") or assignment.correlation_id,
            "correlation_id",
        )
        deadline = _optional_datetime(
            os.getenv("KITSUNE_RUN_DEADLINE") or assignment.deadline, "deadline"
        )
        await self.workspace_client.acknowledge_run(
            run_id, runtime_instance_id=self.runtime_instance_id
        )
        return await self._execute(
            handler,
            input_data,
            run_id=run_id,
            source=source,
            parent_run_id=parent_run_id,
            correlation_id=correlation_id,
            deadline=deadline,
            metadata=None,
            persist_agent_origin=False,
        )

    async def drain_outbox_only(self) -> OutboxDrainResult:
        """Drain a persisted queue without lifecycle, registration, acknowledgement, or Run work."""

        if self._started:
            raise RuntimeError("drain-only recovery requires a stopped application")
        if self.outbox is None or self.workspace_client is None:
            raise RuntimeError("drain-only recovery requires Workspace and Event Outbox settings")
        try:
            result = await self.outbox.drain(
                self.workspace_client.send_events,
                batch_size=self.settings.event_batch_size,
                max_batch_bytes=self.settings.event_max_batch_bytes,
                initial_backoff=self.settings.event_retry_initial_seconds,
                maximum_backoff=self.settings.event_retry_max_seconds,
                deadline_monotonic=monotonic() + self.settings.shutdown_grace_seconds,
            )
        finally:
            await self.workspace_client.close()
        if result.remaining:
            raise EphemeralDeliveryIncompleteError(
                remaining=result.remaining,
                failures=result.failures,
            )
        return result

    def run(self, *, host: str | None = None, port: int | None = None) -> None:
        """Serve the resident Agent Control API with Uvicorn."""

        import uvicorn

        from .control import create_control_api

        effective_host = host or self.settings.bind_host
        effective_port = port or self.settings.bind_port
        uvicorn.run(
            create_control_api(
                self,
                bind_host=effective_host,
                bind_port=effective_port,
            ),
            host=effective_host,
            port=effective_port,
            proxy_headers=False,
        )

    def _make_context(
        self,
        *,
        run_id: UUID,
        parent_run_id: UUID | None,
        correlation_id: UUID,
        source: RunSource,
        started_at: datetime,
        deadline: datetime | None,
        metadata: Mapping[str, Any] | None,
    ) -> RunContext:
        context = RunContext(
            agent_id=self.agent_id,
            runtime_instance_id=self.runtime_instance_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            correlation_id=correlation_id,
            source=source,
            started_at=started_at,
            deadline=deadline,
            cancel_scope=RunCancelScope(),
            logger=contextual_logger(
                self.logger,
                agent_id=self.agent_id,
                runtime_instance_id=str(self.runtime_instance_id),
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                correlation_id=str(correlation_id),
                trace_id=current_trace_id(),
            ),
            tracer=self.telemetry.tracer,
            workspace_client=self.workspace_client,
            metadata=metadata,
            event_emitter=self._deliver_event,
            child_guards=tuple(self._child_guards),
            model_guards=tuple(self._model_guards),
            usage_observers=tuple(self._usage_observers),
            usage_event_reserver=self._reserve_usage_event,
            run_event_capacity_reserver=self._reserve_run_event_capacity,
            run_event_capacity_releaser=self._release_run_event_capacity,
            context_initializer=self._initialize_context,
            child_started=self._child_started,
            child_finished=self._child_finished,
        )
        self._initialize_context(context)
        return context

    def _initialize_context(self, context: RunContext) -> None:
        for name, factory in self._extension_factories.items():
            context.install_extension(name, factory(context))

    async def _reserve_run_event_capacity(self, context: RunContext) -> None:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("Run event capacity requires an asyncio Task")
        if not await self._reserve_run_event_capacity_for_run(context.run_id, current_task):
            raise DuplicateRunError(str(context.run_id))

    async def _reserve_run_event_capacity_for_run(
        self,
        run_id: UUID,
        owner: asyncio.Task[Any] | None,
    ) -> bool:
        if self.outbox is None:
            return True
        async with self._run_outbox_reservation_lock:
            existing = self._run_outbox_reservations.get(run_id)
            if existing is not None:
                return owner is not None and existing.owner is owner
            try:
                lifecycle = await self.outbox.reserve(2)
            except OutboxFullError:
                self._report_outbox_full()
                raise
            self._run_outbox_reservations[run_id] = _RunOutboxReservation(
                lifecycle=lifecycle,
                usage=[],
                owner=owner,
            )
            return True

    async def _reserve_usage_event(self, context: RunContext) -> Callable[[], None] | None:
        if self.outbox is None:
            return None
        try:
            usage = await self._reserve_usage_event_for_run(context.run_id)
        except OutboxFullError:
            self._report_outbox_full()
            raise

        def release() -> None:
            reservation = self._run_outbox_reservations.get(context.run_id)
            if reservation is not None and usage in reservation.usage:
                reservation.usage.remove(usage)
                usage.release()

        return release

    async def _reserve_usage_event_for_run(self, run_id: UUID) -> EventOutboxReservation:
        if self.outbox is None:
            raise RuntimeError("Usage capacity requires an Event Outbox")
        reservation = self._run_outbox_reservations.get(run_id)
        if reservation is None:
            raise RuntimeError("Usage capacity can only be reserved for an admitted Run")
        usage = await self.outbox.reserve(1)
        reservation.usage.append(usage)
        return usage

    def _release_run_event_capacity(self, run_id: UUID) -> None:
        reservation = self._run_outbox_reservations.pop(run_id, None)
        if reservation is None:
            return
        reservation.lifecycle.release()
        for usage in reservation.usage:
            usage.release()

    def _report_outbox_full(self) -> None:
        assert self.outbox is not None
        self.telemetry.event_delivery_failures_total.add(
            1, {"agent_id": self.agent_id, "reason": "outbox_full"}
        )
        self.logger.critical(
            "Kitsune event outbox is full",
            extra={
                "event": "kitsune.event.outbox_full",
                "details": {"capacity": self.outbox.capacity},
            },
        )

    async def _child_started(self, context: RunContext) -> None:
        persisted = self.workspace_client is None
        try:
            if self.workspace_client is not None:
                timeout_seconds = (
                    max(1, int((context.deadline - datetime.now(UTC)).total_seconds()))
                    if context.deadline is not None
                    else None
                )
                await self.workspace_client.begin_run(
                    run_id=context.run_id,
                    agent_id=self.agent_id,
                    runtime_instance_id=self.runtime_instance_id,
                    handler=str(context.metadata["name"]),
                    source=RunSource.CHILD,
                    parent_run_id=context.parent_run_id,
                    correlation_id=context.correlation_id,
                    trace_id=context.trace_id,
                    input_data={"metadata": _json_value(dict(context.metadata))},
                    timeout_seconds=timeout_seconds,
                )
                persisted = True
            await self.emit(
                "kitsune.run.started", ctx=context, payload={"name": context.metadata["name"]}
            )
            await self._plugin_host.on_run_started(context, self._report_plugin_error)
        except BaseException as exc:
            if persisted:
                if isinstance(exc, asyncio.CancelledError):
                    status = RunStatus.CANCELLED
                    error = None
                elif isinstance(exc, TimeoutError):
                    status = RunStatus.TIMED_OUT
                    error = RunError(type=type(exc).__name__, message="Run deadline exceeded")
                else:
                    status = RunStatus.FAILED
                    error = RunError(type=type(exc).__name__, message="Child Run failed")
                outcome = RunOutcome(
                    status=status,
                    error=error,
                    usage=list(context.usage),
                    ended_at=datetime.now(UTC),
                )
                try:
                    await self._child_finished(context, outcome)
                except BaseException as terminal_error:
                    exc.add_note(
                        "Persisted child Run terminal reporting also failed: "
                        f"{type(terminal_error).__name__}"
                    )
                    self.logger.error(
                        "Persisted child Run terminal reporting failed",
                        extra={
                            "event": "kitsune.child_run.terminal_failed",
                            "details": {
                                "run_id": str(context.run_id),
                                "error_type": type(terminal_error).__name__,
                                "message": "Terminal reporting failed",
                            },
                        },
                    )
            else:
                self._release_run_event_capacity(context.run_id)
            raise

    async def _child_finished(self, context: RunContext, outcome: RunOutcome) -> None:
        try:
            try:
                await self._plugin_host.on_run_finished(context, outcome, self._report_plugin_error)
            except CriticalPluginHookError as exc:
                self._log_plugin_cleanup_failure(exc, run_id=context.run_id)
            try:
                await self.emit(
                    f"kitsune.run.{outcome.status.value}",
                    ctx=context,
                    payload={"name": context.metadata["name"]},
                )
            except CriticalPluginHookError as exc:
                self._log_plugin_cleanup_failure(exc, run_id=context.run_id)
        finally:
            self._release_run_event_capacity(context.run_id)

    async def _deliver_event(self, event: KitsuneEvent, notify_plugins: bool) -> None:
        event = event.model_copy(
            update={
                "payload": cast(
                    dict[str, Any],
                    redact_sensitive_data(
                        event.payload,
                        self.settings.redacted_keys,
                        self._redacted_values,
                    ),
                )
            }
        )
        event = _bounded_event(event, maximum_bytes=self.settings.event_max_payload_bytes)
        plugin_error: CriticalPluginHookError | None = None
        if notify_plugins:
            try:
                await self._plugin_host.on_event(event, self._report_plugin_error)
            except CriticalPluginHookError as exc:
                plugin_error = exc
        self.logger.log(
            _severity_level(event.severity),
            event.type,
            extra={
                "agent_id": event.agent_id,
                "runtime_instance_id": str(event.runtime_instance_id)
                if event.runtime_instance_id
                else None,
                "run_id": str(event.run_id) if event.run_id else None,
                "parent_run_id": str(event.parent_run_id) if event.parent_run_id else None,
                "correlation_id": str(event.correlation_id) if event.correlation_id else None,
                "trace_id": event.trace_id,
                "event": event.type,
                "details": event.payload,
            },
        )
        if self.outbox is not None:
            try:
                reservation = (
                    self._run_outbox_reservations.get(event.run_id)
                    if event.run_id is not None
                    else None
                )
                if (
                    reservation is not None
                    and event.type == "kitsune.run.started"
                    and not reservation.started_enqueued
                ):
                    inserted = await reservation.lifecycle.enqueue(event)
                    reservation.started_enqueued = True
                elif (
                    reservation is not None
                    and event.type in _TERMINAL_RUN_EVENT_TYPES
                    and not reservation.terminal_enqueued
                ):
                    inserted = await reservation.lifecycle.enqueue(event)
                    reservation.terminal_enqueued = True
                elif reservation is not None and event.type in {
                    "kitsune.usage",
                    "kitsune.model.usage",
                }:
                    if not reservation.usage:
                        assert event.run_id is not None
                        await self._reserve_usage_event_for_run(event.run_id)
                    usage_reservation = reservation.usage.pop(0)
                    try:
                        inserted = await usage_reservation.enqueue(event)
                    finally:
                        usage_reservation.release()
                else:
                    inserted = await self.outbox.enqueue(event)
            except OutboxFullError:
                self._report_outbox_full()
                raise
            if inserted:
                self.telemetry.event_outbox_size.add(1, {"agent_id": self.agent_id})
        if plugin_error is not None:
            raise plugin_error

    async def _report_plugin_error(
        self, plugin: KitsunePlugin, hook: str, exception: Exception
    ) -> None:
        event = KitsuneEvent(
            type="kitsune.plugin.failed",
            occurred_at=datetime.now(UTC),
            agent_id=self.agent_id,
            runtime_instance_id=self.runtime_instance_id,
            severity=EventSeverity.ERROR,
            payload={
                "plugin": plugin.metadata.name,
                "hook": hook,
                "error_type": type(exception).__name__,
                "message": "Plugin hook failed",
            },
        )
        try:
            await self._plugin_host.on_event(
                event,
                self._log_nested_plugin_error,
                exclude=frozenset({plugin.metadata.name}),
            )
        except CriticalPluginHookError as nested_error:
            self._log_plugin_cleanup_failure(nested_error)
        try:
            await self._deliver_event(event, notify_plugins=False)
        except OutboxFullError:
            self.logger.error(
                "Plugin failure event could not enter the full outbox",
                extra={
                    "event": "kitsune.plugin.failure_not_queued",
                    "details": {"plugin": plugin.metadata.name, "hook": hook},
                },
            )

    async def _log_nested_plugin_error(
        self, plugin: KitsunePlugin, hook: str, exception: Exception
    ) -> None:
        self.logger.error(
            "Plugin failed while observing another Plugin failure",
            extra={
                "event": "kitsune.plugin.failure_observer_failed",
                "details": {
                    "plugin": plugin.metadata.name,
                    "hook": hook,
                    "error_type": type(exception).__name__,
                    "message": "Plugin hook failed",
                },
            },
        )

    def _log_plugin_cleanup_failure(
        self, exception: CriticalPluginHookError, *, run_id: UUID | None = None
    ) -> None:
        self.logger.error(
            "Critical Plugin observation failed after core state was decided",
            extra={
                "event": "kitsune.plugin.cleanup_failed",
                "details": {
                    "plugin": exception.plugin.name,
                    "hook": exception.hook,
                    "run_id": str(run_id) if run_id else None,
                    "error_type": type(exception.cause).__name__,
                    "message": "Plugin hook failed",
                },
            },
        )

    def _record_shutdown_error(
        self,
        errors: list[tuple[str, BaseException]],
        phase: str,
        exception: BaseException,
    ) -> None:
        errors.append((phase, exception))
        self.logger.error(
            "Kitsune shutdown phase failed; cleanup will continue",
            extra={
                "event": "kitsune.agent.shutdown_phase_failed",
                "details": {
                    "phase": phase,
                    "error_type": type(exception).__name__,
                    "message": "Shutdown phase failed",
                },
            },
        )

    async def _record_usage_metrics(self, context: RunContext, usage: UsageRecord) -> None:
        attributes = {"agent_id": self.agent_id}
        if usage.request_count is not None:
            self.telemetry.model_requests_total.add(usage.request_count, attributes)
        if usage.input_tokens is not None:
            self.telemetry.model_input_tokens_total.add(usage.input_tokens, attributes)
        if usage.output_tokens is not None:
            self.telemetry.model_output_tokens_total.add(usage.output_tokens, attributes)
        if usage.estimated_cost is not None:
            self.telemetry.model_estimated_cost.add(float(usage.estimated_cost), attributes)
        await context.emit("kitsune.model.usage", payload=usage.model_dump(mode="json"))

    def _spawn_background(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _delivery_loop(self) -> None:
        assert self.outbox is not None
        assert self.workspace_client is not None
        while not self._delivery_stop.is_set():
            try:
                delivered = await self.outbox.flush_once(
                    self.workspace_client.send_events,
                    batch_size=self.settings.event_batch_size,
                    max_batch_bytes=self.settings.event_max_batch_bytes,
                    initial_backoff=self.settings.event_retry_initial_seconds,
                    maximum_backoff=self.settings.event_retry_max_seconds,
                )
                if delivered:
                    self.telemetry.event_outbox_size.add(-delivered, {"agent_id": self.agent_id})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.telemetry.event_delivery_failures_total.add(1, {"agent_id": self.agent_id})
                self.logger.warning(
                    "Workspace event delivery failed",
                    extra={
                        "event": "kitsune.event.delivery_failed",
                        "details": {
                            "error_type": type(exc).__name__,
                            "message": "Workspace event delivery failed",
                        },
                    },
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._delivery_stop.wait(), timeout=0.25)

    async def _heartbeat_loop(self) -> None:
        assert self.workspace_client is not None
        while not self._heartbeat_stop.is_set():
            heartbeat = AgentHeartbeat(
                agent_id=self.agent_id,
                runtime_instance_id=self.runtime_instance_id,
                occurred_at=datetime.now(UTC),
                active_runs=len(self._active_runs),
            )
            try:
                await self.workspace_client.heartbeat(heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "Workspace heartbeat failed",
                    extra={
                        "event": "kitsune.heartbeat.failed",
                        "details": {
                            "error_type": type(exc).__name__,
                            "message": "Workspace heartbeat failed",
                        },
                    },
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._heartbeat_stop.wait(), timeout=self.settings.heartbeat_interval_seconds
                )


def _severity_level(severity: EventSeverity) -> int:
    return {
        EventSeverity.DEBUG: logging.DEBUG,
        EventSeverity.INFO: logging.INFO,
        EventSeverity.WARNING: logging.WARNING,
        EventSeverity.ERROR: logging.ERROR,
        EventSeverity.CRITICAL: logging.CRITICAL,
    }[severity]


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for ephemeral execution")
    return value


def _optional_uuid(value: Any, field_name: str) -> UUID | None:
    if value in {None, ""}:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid {field_name}: expected UUID") from exc


def _optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"invalid {field_name}: expected RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"invalid {field_name}: timezone is required")
    return parsed.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return [_json_value(item) for item in sequence]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return str(value)


def _bounded_event(event: KitsuneEvent, *, maximum_bytes: int) -> KitsuneEvent:
    payload = cast(dict[str, Any], _json_value(event.payload))
    size = _json_size(payload)
    if size <= maximum_bytes:
        return event.model_copy(update={"payload": payload})

    terminal = event.type in _TERMINAL_RUN_EVENT_TYPES
    if terminal:
        error_type = "OutputTooLarge" if "output" in payload else "EventPayloadTooLarge"
        bounded_payload: dict[str, Any] = {
            "error": RunError(
                type=error_type,
                message="Run terminal event exceeded the configured payload limit",
                retryable=False,
                details={"size_bytes": size, "maximum_bytes": maximum_bytes},
            ).model_dump(mode="json"),
            "payload_rejected": {
                "reason": "too_large",
                "size_bytes": size,
                "maximum_bytes": maximum_bytes,
            },
        }
        handler = payload.get("handler")
        if isinstance(handler, str):
            bounded_payload["handler"] = handler
        return event.model_copy(
            update={
                "type": "kitsune.run.failed",
                "severity": EventSeverity.ERROR,
                "payload": bounded_payload,
            }
        )

    return event.model_copy(
        update={
            "type": "kitsune.event.rejected",
            "severity": EventSeverity.ERROR,
            "payload": {
                "rejected_event_type": event.type,
                "reason": "payload_too_large",
                "size_bytes": size,
                "maximum_bytes": maximum_bytes,
            },
        }
    )


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
