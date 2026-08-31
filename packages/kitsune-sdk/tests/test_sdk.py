"""SDK lifecycle, execution, cancellation, Plugin, outbox, and mode tests."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from kitsune_contracts import (
    AgentHeartbeat,
    AgentRegistration,
    AgentRunAssignment,
    KitsuneEvent,
    RunSource,
    RuntimeMode,
)
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr
from typer.testing import CliRunner

from kitsune import (
    EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE,
    EphemeralDeliveryIncompleteError,
    EventOutbox,
    KitsuneApp,
    KitsuneSettings,
    NonRetryableEventDeliveryError,
    OutboxFullError,
    PluginDependencyError,
    PluginMetadata,
    RetryableEventDeliveryError,
    RunContext,
    RunOutcome,
    UsageRecord,
    WorkspaceClient,
    create_control_api,
)
from kitsune.cli import app as cli_app
from kitsune.outbox import _require_private_directory as require_private_outbox_directory


class Request(BaseModel):
    """Simple test Handler input."""

    value: str


class Result(BaseModel):
    """Simple test Handler output."""

    value: str


def test_empty_custom_redacted_keys_cannot_disable_sdk_baseline() -> None:
    settings = KitsuneSettings(redacted_keys=frozenset())

    assert {
        "authorization",
        "cookie",
        "api_key",
        "private_key",
        "password",
        "secret",
        "token",
        "credential",
    } <= settings.redacted_keys


@pytest.mark.parametrize("token", ["", " \t\n"])
def test_settings_rejects_empty_agent_token(token: str) -> None:
    """A configured credential cannot compare equal to a missing Bearer value."""

    with pytest.raises(ValueError, match="must not be empty"):
        KitsuneSettings(agent_token=SecretStr(token))


def test_workspace_transport_requires_tls_or_explicit_nonloopback_opt_in() -> None:
    token = SecretStr("agent-token")
    with pytest.raises(ValueError, match="ALLOW_INSECURE_WORKSPACE"):
        KitsuneSettings(
            workspace_url=cast(AnyHttpUrl, "http://workspace.example.invalid"),
            agent_token=token,
        )
    with pytest.raises(ValueError, match="ALLOW_INSECURE_WORKSPACE"):
        WorkspaceClient("http://workspace.example.invalid", "agent-token")

    loopback = KitsuneSettings(
        workspace_url=cast(AnyHttpUrl, "http://127.0.0.1:8080"),
        agent_token=token,
    )
    opted_in = KitsuneSettings(
        workspace_url=cast(AnyHttpUrl, "http://workspace.example.invalid"),
        agent_token=token,
        allow_insecure_workspace=True,
    )
    assert loopback.managed and opted_in.managed


@pytest.mark.parametrize("token", ["", " \t\n"])
def test_workspace_client_rejects_empty_token(token: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        WorkspaceClient("https://workspace.invalid", token)


def test_manifest_cli_does_not_echo_invalid_literal_value(tmp_path: Path) -> None:
    """Manifest validation reports only the controlled exception type."""

    sentinel = "sdk-manifest-literal-secret"
    manifest_path = tmp_path / "invalid-agent.yaml"
    manifest_path.write_text(
        f"schema: kitsune.agent\nrevision: {sentinel}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_app, ["manifest", "validate", str(manifest_path)])

    assert result.exit_code == 1
    assert "Manifest validation failed (ValidationError)" in result.output
    assert sentinel not in result.output


class CapturePlugin:
    """Record public lifecycle hooks for assertions."""

    def __init__(
        self,
        name: str = "capture",
        *,
        dependencies: tuple[str, ...] = (),
        critical: bool = False,
        fail_hook: str | None = None,
    ) -> None:
        self.metadata = PluginMetadata(
            name=name, version="1.0.0", dependencies=dependencies, critical=critical
        )
        self.fail_hook = fail_hook
        self.lifecycle: list[str] = []
        self.contexts: list[RunContext] = []
        self.outcomes: list[RunOutcome] = []
        self.events: list[KitsuneEvent] = []

    def configure(self, app: Any) -> None:
        self.lifecycle.append(f"configure:{app.agent_id}")

    async def start(self, app: Any) -> None:
        if self.fail_hook == "start":
            raise RuntimeError("start failed")
        self.lifecycle.append(f"start:{app.agent_id}")

    async def stop(self, app: Any) -> None:
        self.lifecycle.append(f"stop:{app.agent_id}")

    async def on_run_started(self, ctx: RunContext) -> None:
        if self.fail_hook == "on_run_started":
            raise RuntimeError("observation failed")
        self.contexts.append(ctx)

    async def on_run_finished(self, ctx: RunContext, outcome: RunOutcome) -> None:
        self.outcomes.append(outcome)
        if self.fail_hook == "on_run_finished":
            raise RuntimeError("finish observation failed")

    async def on_event(self, event: KitsuneEvent) -> None:
        if self.fail_hook == "on_event":
            raise RuntimeError("event observation failed")
        self.events.append(event)


class FakeWorkspaceClient:
    """In-memory implementation of the public Workspace Client behavior."""

    def __init__(self, assignment: dict[str, Any] | None = None) -> None:
        self.assignment = assignment or {"input": {"value": "managed"}}
        self.registrations: list[AgentRegistration] = []
        self.heartbeats: list[AgentHeartbeat] = []
        self.batches: list[list[KitsuneEvent]] = []
        self.acks: list[tuple[UUID, UUID]] = []
        self.begins: list[dict[str, Any]] = []
        self.closed = False
        self.heartbeat_sent = asyncio.Event()
        self._delivery_condition = asyncio.Condition()

    async def register(self, registration: AgentRegistration) -> dict[str, Any]:
        self.registrations.append(registration)
        return {"status": "registered"}

    async def heartbeat(self, heartbeat: AgentHeartbeat) -> None:
        self.heartbeats.append(heartbeat)
        self.heartbeat_sent.set()

    async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
        async with self._delivery_condition:
            self.batches.append(list(events))
            self._delivery_condition.notify_all()

    async def wait_for_event(self, event_type: str, *, run_id: UUID | None = None) -> None:
        """Wait until a matching event has been delivered to the fake Workspace."""

        def delivered() -> bool:
            return any(
                event.type == event_type and (run_id is None or event.run_id == run_id)
                for batch in self.batches
                for event in batch
            )

        async with self._delivery_condition:
            await self._delivery_condition.wait_for(delivered)

    async def begin_run(self, **values: Any) -> dict[str, Any]:
        self.begins.append(values)
        return {"run_id": str(values["run_id"]), "status": "running"}

    async def get_run_assignment(self, run_id: UUID) -> AgentRunAssignment:
        return AgentRunAssignment.model_validate(
            {
                "run_id": run_id,
                "agent_id": "test-agent",
                "handler": "echo",
                "source": "on_demand",
                "correlation_id": uuid4(),
                **self.assignment,
            }
        )

    async def acknowledge_run(self, run_id: UUID, *, runtime_instance_id: UUID) -> None:
        self.acks.append((run_id, runtime_instance_id))

    async def close(self) -> None:
        self.closed = True


def make_app(
    tmp_path: Path,
    *,
    workspace: FakeWorkspaceClient | None = None,
    shutdown_grace: float = 30,
    agent_token: str | None = None,
    bind_host: str = "127.0.0.1",
    control_max_request_bytes: int = 1_048_576,
) -> KitsuneApp:
    """Create a test SDK application with a task-owned outbox."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "events.sqlite3",
        heartbeat_interval_seconds=0.01,
        shutdown_grace_seconds=shutdown_grace,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.02,
        agent_token=SecretStr(agent_token) if agent_token is not None else None,
        bind_host=bind_host,
        control_max_request_bytes=control_max_request_bytes,
    )
    return KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path) if workspace else None,
    )


def add_echo_handler(application: KitsuneApp) -> None:
    """Register one deterministic generic callable Handler."""

    @application.handler("echo", input_model=Request, output_model=Result)
    async def echo(ctx: RunContext, request: Request) -> Result:
        ctx.raise_if_cancelled()
        return Result(value=request.value)


@pytest.mark.asyncio
async def test_standalone_lifecycle_handler_and_plugin_hooks(tmp_path: Path) -> None:
    """Standalone Mode executes typed Handlers without Workspace registration."""

    capture = CapturePlugin()
    application = make_app(tmp_path).use(capture)
    add_echo_handler(application)

    async with application:
        result = await application.execute("echo", {"value": "local"})
        assert result == Result(value="local")
        assert application.descriptor.handlers[0].input_schema["type"] == "object"

    assert capture.lifecycle == ["configure:test-agent", "start:test-agent", "stop:test-agent"]
    assert capture.outcomes[0].status.value == "succeeded"
    assert {event.type for event in capture.events} >= {
        "kitsune.agent.started",
        "kitsune.run.started",
        "kitsune.run.succeeded",
    }


@pytest.mark.asyncio
async def test_cancellation_reaches_handler_and_terminal_outcome(tmp_path: Path) -> None:
    """Cancellation interrupts an active Handler and produces a cancelled outcome."""

    application = make_app(tmp_path)
    capture = CapturePlugin()
    application.use(capture)
    started = asyncio.Event()

    @application.handler("wait", input_model=Request, output_model=Result)
    async def wait_forever(ctx: RunContext, request: Request) -> Result:
        started.set()
        await asyncio.Event().wait()
        return Result(value=request.value)

    run_id = uuid4()
    async with application:
        task = await application.submit("wait", {"value": "x"}, run_id=run_id)
        await started.wait()
        assert await application.cancel(run_id)
        with pytest.raises(asyncio.CancelledError):
            await task

    assert capture.outcomes[-1].status.value == "cancelled"


@pytest.mark.asyncio
async def test_parent_child_run_inherits_lineage_deadline_and_cancellation(tmp_path: Path) -> None:
    """Child Runs inherit correlation, parent, deadline, and cancellation ancestry."""

    application = make_app(tmp_path)
    capture = CapturePlugin()
    application.use(capture)
    observed: dict[str, Any] = {}

    @application.handler("parent", input_model=Request, output_model=Result)
    async def parent(ctx: RunContext, request: Request) -> Result:
        async with ctx.child_run(name="child") as child:
            observed.update(
                parent_run_id=child.parent_run_id,
                correlation_id=child.correlation_id,
                deadline=child.deadline,
                child_run_id=child.run_id,
            )
        return Result(value=request.value)

    run_id = uuid4()
    correlation_id = uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    async with application:
        await application.execute(
            "parent",
            {"value": "ok"},
            run_id=run_id,
            correlation_id=correlation_id,
            deadline=deadline,
        )

    assert observed["parent_run_id"] == run_id
    assert observed["correlation_id"] == correlation_id
    assert observed["deadline"] == deadline
    assert observed["child_run_id"] != run_id


@pytest.mark.asyncio
async def test_timeout_outcome_preserves_typed_error(tmp_path: Path) -> None:
    """Timed-out Runs retain TimeoutError details in Plugin outcomes and Events."""

    application = make_app(tmp_path)
    capture = CapturePlugin()
    application.use(capture)

    @application.handler(
        "slow", input_model=Request, output_model=Result, default_timeout_seconds=1
    )
    async def slow(ctx: RunContext, request: Request) -> Result:
        await asyncio.sleep(1)
        return Result(value=request.value)

    async with application:
        with pytest.raises(TimeoutError):
            await application.execute(
                "slow", {"value": "late"}, deadline=datetime.now(UTC) + timedelta(milliseconds=5)
            )

    outcome = capture.outcomes[-1]
    assert outcome.status.value == "timed_out"
    assert outcome.error is not None and outcome.error.type == "TimeoutError"
    timeout_event = next(event for event in capture.events if event.type == "kitsune.run.timed_out")
    assert timeout_event.payload["error"]["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_noncritical_plugin_failure_isolated_and_reported(tmp_path: Path) -> None:
    """A noncritical observation failure does not fail Handler execution."""

    application = make_app(tmp_path)
    failing = CapturePlugin(name="failing", fail_hook="on_run_started")
    capture = CapturePlugin()
    application.use(failing).use(capture)
    add_echo_handler(application)

    async with application:
        result = Result.model_validate(await application.execute("echo", {"value": "still-runs"}))

    assert result.value == "still-runs"
    failure = next(event for event in capture.events if event.type == "kitsune.plugin.failed")
    assert failure.payload["plugin"] == "failing"


@pytest.mark.asyncio
async def test_plugin_exception_text_never_reaches_observers_or_sqlite_outbox(
    tmp_path: Path,
) -> None:
    """Plugin failure reporting preserves the type without persisting exception text."""

    secret = "plugin-failure-sentinel"

    class SecretFailurePlugin(CapturePlugin):
        async def on_event(self, event: KitsuneEvent) -> None:
            del event
            raise RuntimeError(f"Authorization: Bearer {secret}")

    settings = KitsuneSettings(outbox_path=tmp_path / "plugin.sqlite3")
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
    )
    observer = CapturePlugin("observer")
    application.use(SecretFailurePlugin("failing")).use(observer)

    await application.startup()
    try:
        pending = await outbox.pending(limit=20)
    finally:
        await application.shutdown()

    failure = next(event for event in observer.events if event.type == "kitsune.plugin.failed")
    serialized_observer = json.dumps(failure.model_dump(mode="json"), sort_keys=True)
    serialized_outbox = json.dumps(
        [item.event.model_dump(mode="json") for item in pending], sort_keys=True
    )
    assert secret not in serialized_observer
    assert secret not in serialized_outbox
    assert failure.payload["error_type"] == "RuntimeError"
    assert failure.payload["message"] == "Plugin hook failed"


@pytest.mark.asyncio
async def test_plugin_event_payload_mutation_is_isolated_from_observers_and_outbox(
    tmp_path: Path,
) -> None:
    """A shallow-frozen Event cannot let a Plugin rewrite another consumer's payload."""

    class MutatingPlugin(CapturePlugin):
        async def on_event(self, event: KitsuneEvent) -> None:
            event.payload["value"]["nested"] = "tampered"
            event.payload["injected"] = "plugin-secret"

    settings = KitsuneSettings(outbox_path=tmp_path / "plugin-mutation.sqlite3")
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
    )
    observer = CapturePlugin("observer")
    application.use(MutatingPlugin("mutator")).use(observer)

    await application.startup()
    try:
        emitted = await application.emit(
            "kitsune.custom.payload",
            payload={"value": {"nested": "original"}},
        )
        pending = await outbox.pending(limit=100)
    finally:
        await application.shutdown()

    observed = next(event for event in observer.events if event.event_id == emitted.event_id)
    persisted = next(item.event for item in pending if item.event.event_id == emitted.event_id)
    assert observed.payload == {"value": {"nested": "original"}}
    assert persisted.payload == {"value": {"nested": "original"}}


@pytest.mark.asyncio
async def test_plugin_dependency_cycle_and_critical_start_failure_reject_startup(
    tmp_path: Path,
) -> None:
    """Cycles and critical startup failures prevent application startup."""

    cyclic = make_app(tmp_path / "cycle")
    cyclic.use(CapturePlugin("a", dependencies=("b",))).use(CapturePlugin("b", dependencies=("a",)))
    with pytest.raises(PluginDependencyError):
        await cyclic.startup()

    critical = make_app(tmp_path / "critical")
    dependency = CapturePlugin("dependency")
    failing = CapturePlugin(
        "critical",
        dependencies=("dependency",),
        critical=True,
        fail_hook="start",
    )
    critical.use(dependency).use(failing)
    with pytest.raises(RuntimeError, match="start failed"):
        await critical.startup()
    assert dependency.lifecycle == [
        "configure:test-agent",
        "start:test-agent",
        "stop:test-agent",
    ]

    failing.fail_hook = None
    await critical.startup()
    assert critical.started
    await critical.shutdown()
    assert dependency.lifecycle[-2:] == ["start:test-agent", "stop:test-agent"]


@pytest.mark.asyncio
async def test_critical_finish_hook_does_not_override_terminal_run_outcome(
    tmp_path: Path,
) -> None:
    """A critical cleanup observer cannot replace a completed Handler result."""

    application = make_app(tmp_path)
    failing = CapturePlugin("critical-finish", critical=True, fail_hook="on_run_finished")
    observer = CapturePlugin("observer")
    application.use(failing).use(observer)
    add_echo_handler(application)

    async with application:
        result = Result.model_validate(await application.execute("echo", {"value": "complete"}))

    assert result.value == "complete"
    assert observer.outcomes[-1].status.value == "succeeded"
    assert any(event.type == "kitsune.run.succeeded" for event in observer.events)
    failure = next(
        event
        for event in observer.events
        if event.type == "kitsune.plugin.failed" and event.payload["plugin"] == "critical-finish"
    )
    assert failure.payload["hook"] == "on_run_finished"


@pytest.mark.asyncio
async def test_plugin_failure_events_do_not_recurse_between_failing_observers(
    tmp_path: Path,
) -> None:
    """Two failing event observers produce bounded failure reports without recursion."""

    application = make_app(tmp_path)
    first = CapturePlugin("first", fail_hook="on_event")
    second = CapturePlugin("second", fail_hook="on_event")
    observer = CapturePlugin("observer")
    application.use(first).use(second).use(observer)

    async with application:
        assert application.started

    failures = {
        event.payload["plugin"]
        for event in observer.events
        if event.type == "kitsune.plugin.failed"
    }
    assert failures == {"first", "second"}


@pytest.mark.asyncio
async def test_event_outbox_dedup_retry_delivery_and_capacity(tmp_path: Path) -> None:
    """SQLite outbox deduplicates IDs, retries failures, and reports capacity."""

    outbox = EventOutbox(tmp_path / "outbox.sqlite3", capacity=1)
    event = KitsuneEvent(
        type="kitsune.run.started",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    assert await outbox.enqueue(event)
    assert not await outbox.enqueue(event)
    with pytest.raises(OutboxFullError):
        await outbox.enqueue(event.model_copy(update={"event_id": uuid4()}))

    calls = 0

    async def sender(events: Sequence[KitsuneEvent]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("workspace unavailable")
        assert events == [event]

    with pytest.raises(httpx.ConnectError):
        await outbox.flush_once(sender, batch_size=10, initial_backoff=0.001, maximum_backoff=0.001)
    await asyncio.sleep(0.005)
    assert (
        await outbox.flush_once(sender, batch_size=10, initial_backoff=0.001, maximum_backoff=0.001)
        == 1
    )
    assert await outbox.size() == 0


@pytest.mark.asyncio
async def test_event_outbox_enforces_exact_bytes_reservations_and_batch_prefix(
    tmp_path: Path,
) -> None:
    first = KitsuneEvent(
        type="kitsune.custom.progress",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
        payload={"value": "same-size"},
    )
    second = first.model_copy(update={"event_id": uuid4()})
    event_bytes = len(first.model_dump_json().encode())

    exact = EventOutbox(
        tmp_path / "exact-bytes.sqlite3",
        capacity=2,
        max_bytes=event_bytes,
        max_event_bytes=event_bytes,
    )
    assert await exact.enqueue(first)
    with pytest.raises(OutboxFullError, match="serialized bytes"):
        await exact.enqueue(second)

    per_event = EventOutbox(
        tmp_path / "per-event-bytes.sqlite3",
        capacity=2,
        max_bytes=event_bytes * 2,
        max_event_bytes=event_bytes - 1,
    )
    with pytest.raises(OutboxFullError, match="serialized bytes"):
        await per_event.enqueue(first)

    reserved = EventOutbox(
        tmp_path / "reserved-bytes.sqlite3",
        capacity=3,
        max_bytes=event_bytes * 2,
        max_event_bytes=event_bytes,
    )
    reservation = await reserved.reserve(1)
    assert await reserved.enqueue(first)
    with pytest.raises(OutboxFullError, match="serialized bytes"):
        await reserved.enqueue(second)
    assert await reservation.enqueue(second)
    pending = await reserved.pending(limit=2, max_batch_bytes=event_bytes)
    assert [item.event.event_id for item in pending] == [first.event_id]
    reservation.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_event_outbox_forces_private_directory_database_wal_and_shm_modes(
    tmp_path: Path,
) -> None:
    """A permissive process umask cannot make durable Agent payloads world-readable."""

    path = tmp_path / "private-outbox" / "events.sqlite3"
    previous_umask = os.umask(0)
    try:
        outbox = EventOutbox(path)
        connection = outbox._connect()
        connection.execute("CREATE TABLE permission_probe (value TEXT)")
        connection.execute("INSERT INTO permission_probe VALUES ('probe')")
    finally:
        os.umask(previous_umask)

    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            assert candidate.exists(), candidate
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_event_outbox_rejects_missing_nested_parent_without_creating_shared_directories(
    tmp_path: Path,
) -> None:
    """Kitsune must not let a permissive umask expose auto-created ancestors."""

    missing_ancestor = tmp_path / "missing-ancestor"
    path = missing_ancestor / "private-outbox" / "events.sqlite3"
    previous_umask = os.umask(0)
    try:
        with pytest.raises(RuntimeError, match="parent must already exist"):
            EventOutbox(path)
    finally:
        os.umask(previous_umask)

    assert not missing_ancestor.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX path security is required")
def test_event_outbox_validates_existing_ancestor_before_creating_private_directory(
    tmp_path: Path,
) -> None:
    """The one permitted parent must not be writable by others or be a symlink."""

    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir(mode=0o700)
    shared_parent.chmod(0o777)
    private_path = shared_parent / "private-outbox" / "events.sqlite3"

    with pytest.raises(RuntimeError, match=r"parent.*group/world-writable"):
        EventOutbox(private_path)

    assert not private_path.parent.exists()
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target_parent, target_is_directory=True)
    linked_path = linked_parent / "private-outbox" / "events.sqlite3"

    with pytest.raises(RuntimeError, match="parent must be a non-symlink directory"):
        EventOutbox(linked_path)

    assert not (target_parent / "private-outbox").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are platform-specific")
def test_event_outbox_rejects_symlink_directory_and_database(tmp_path: Path) -> None:
    """SQLite must never follow a caller-controlled final storage symlink."""

    target_directory = tmp_path / "target"
    target_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(target_directory, target_is_directory=True)
    with pytest.raises(RuntimeError, match="non-symlink directory"):
        EventOutbox(linked_directory / "events.sqlite3")

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    target_file = tmp_path / "target.sqlite3"
    target_file.touch(mode=0o600)
    linked_file = private_directory / "events.sqlite3"
    linked_file.symlink_to(target_file)
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        EventOutbox(linked_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_event_outbox_never_chmods_preexisting_or_shared_parent_directories(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "caller-owned"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    before = stat.S_IMODE(parent.stat().st_mode)

    outbox = EventOutbox(parent / "events.sqlite3")

    assert stat.S_IMODE(parent.stat().st_mode) == before == 0o755
    assert stat.S_IMODE(outbox.path.stat().st_mode) == 0o600
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir()
    shared_directory.chmod(0o777)
    workspace_mode = stat.S_IMODE(shared_directory.stat().st_mode)
    with pytest.raises(RuntimeError, match="group/world-writable"):
        require_private_outbox_directory(shared_directory)
    assert stat.S_IMODE(shared_directory.stat().st_mode) == workspace_mode
    temporary_mode = stat.S_IMODE(Path("/tmp").stat().st_mode)
    with pytest.raises(RuntimeError, match=r"not owned|group/world-writable"):
        require_private_outbox_directory(Path("/tmp"))
    assert stat.S_IMODE(Path("/tmp").stat().st_mode) == temporary_mode


@pytest.mark.asyncio
async def test_reserved_run_and_usage_events_fill_hard_cap_without_losing_terminal(
    tmp_path: Path,
) -> None:
    """Run admission reserves exact started, Usage, and terminal slots during an outage."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "reserved.sqlite3",
        outbox_capacity=4,
        shutdown_grace_seconds=0,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )
    provider_calls = 0

    @application.handler("usage", input_model=Request, output_model=Result)
    async def usage(ctx: RunContext, request: Request) -> Result:
        nonlocal provider_calls
        await ctx.check_model_call()
        provider_calls += 1
        await ctx.record_usage(UsageRecord(provider="offline", total_tokens=3))
        return Result(value=request.value)

    await application.startup()
    result = await application.execute("usage", {"value": "complete"})

    assert result == Result(value="complete")
    assert provider_calls == 1
    with sqlite3.connect(settings.outbox_path) as connection:
        payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM event_outbox ORDER BY created_at, event_id"
            )
        ]
    assert [payload["type"] for payload in payloads] == [
        "kitsune.agent.started",
        "kitsune.run.started",
        "kitsune.model.usage",
        "kitsune.run.succeeded",
    ]
    with pytest.raises(OutboxFullError):
        await application.shutdown()


@pytest.mark.asyncio
async def test_usage_capacity_rejection_precedes_provider_and_preserves_terminal(
    tmp_path: Path,
) -> None:
    """A provider is not called when its Usage Event cannot be durably admitted."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "usage-refused.sqlite3",
        outbox_capacity=3,
        shutdown_grace_seconds=0,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path, capacity=3),
    )
    provider_calls = 0

    @application.handler("model", input_model=Request, output_model=Result)
    async def model(ctx: RunContext, request: Request) -> Result:
        nonlocal provider_calls
        await ctx.check_model_call()
        provider_calls += 1
        return Result(value=request.value)

    await application.startup()
    with pytest.raises(OutboxFullError):
        await application.execute("model", {"value": "blocked"})

    assert provider_calls == 0
    with sqlite3.connect(settings.outbox_path) as connection:
        payloads = [
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM event_outbox")
        ]
    terminal = next(payload for payload in payloads if payload["type"] == "kitsune.run.failed")
    assert terminal["payload"]["error"]["type"] == "OutboxFullError"
    with pytest.raises(OutboxFullError):
        await application.shutdown()


@pytest.mark.asyncio
async def test_unused_usage_reservation_returns_to_hard_cap_after_terminal(tmp_path: Path) -> None:
    """A provider attempt without Usage releases its held slot when the Run ends."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "unused-usage.sqlite3",
        outbox_capacity=4,
        shutdown_grace_seconds=0,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )

    @application.handler("no-usage", input_model=Request, output_model=Result)
    async def no_usage(ctx: RunContext, request: Request) -> Result:
        await ctx.check_model_call()
        return Result(value=request.value)

    await application.startup()
    await application.execute("no-usage", {"value": "complete"})
    await application.emit("kitsune.custom.after-run")

    assert application.outbox is not None
    assert await application.outbox.size() == 4
    with pytest.raises(OutboxFullError):
        await application.shutdown()


@pytest.mark.asyncio
async def test_failed_provider_releases_usage_slot_for_tight_cap_fallback(tmp_path: Path) -> None:
    """A failed provider admission does not starve a later fallback in the same Run."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "fallback-capacity.sqlite3",
        outbox_capacity=4,
        shutdown_grace_seconds=0,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )
    provider_calls: list[str] = []

    @application.handler("fallback", input_model=Request, output_model=Result)
    async def fallback(ctx: RunContext, request: Request) -> Result:
        primary = await ctx.check_model_call()
        provider_calls.append("primary")
        primary.release()
        await ctx.check_model_call()
        provider_calls.append("fallback")
        await ctx.record_usage(UsageRecord(provider="fallback", total_tokens=2))
        return Result(value=request.value)

    await application.startup()
    result = await application.execute("fallback", {"value": "complete"})

    assert result == Result(value="complete")
    assert provider_calls == ["primary", "fallback"]
    assert application.outbox is not None
    pending = await application.outbox.pending(limit=10)
    assert [item.event.type for item in pending] == [
        "kitsune.agent.started",
        "kitsune.run.started",
        "kitsune.model.usage",
        "kitsune.run.succeeded",
    ]
    await application.outbox.mark_delivered([item.event.event_id for item in pending])
    await application.shutdown()


@pytest.mark.asyncio
async def test_direct_usage_jit_capacity_failure_still_persists_run_terminal(
    tmp_path: Path,
) -> None:
    """Direct Usage without preflight fails explicitly while its terminal slot remains held."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "direct-usage.sqlite3",
        outbox_capacity=3,
        shutdown_grace_seconds=0,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path, capacity=3),
    )

    @application.handler("direct", input_model=Request, output_model=Result)
    async def direct(ctx: RunContext, request: Request) -> Result:
        await ctx.record_usage(UsageRecord(total_tokens=7))
        return Result(value=request.value)

    await application.startup()
    with pytest.raises(OutboxFullError):
        await application.execute("direct", {"value": "blocked"})

    with sqlite3.connect(settings.outbox_path) as connection:
        payloads = [
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM event_outbox")
        ]
    terminal = next(payload for payload in payloads if payload["type"] == "kitsune.run.failed")
    assert terminal["payload"]["error"]["type"] == "OutboxFullError"
    assert "usage" not in terminal["payload"]
    with pytest.raises(OutboxFullError):
        await application.shutdown()


@pytest.mark.asyncio
async def test_multiple_bounded_usage_events_do_not_rewrite_successful_terminal(
    tmp_path: Path,
) -> None:
    """Terminal payload stays small because dedicated Usage Events own exact Usage records."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "multi-usage.sqlite3",
        outbox_capacity=5,
        event_max_payload_bytes=1024,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=5),
    )

    @application.handler("usage", input_model=Request, output_model=Result)
    async def usage(ctx: RunContext, request: Request) -> Result:
        for provider in ("a" * 255, "b" * 255):
            await ctx.check_model_call()
            await ctx.record_usage(UsageRecord(provider=provider, total_tokens=1))
        return Result(value=request.value)

    await application.startup()
    result = await application.execute("usage", {"value": "complete"})

    assert result == Result(value="complete")
    assert application.outbox is not None
    pending = await application.outbox.pending(limit=10)
    assert [item.event.type for item in pending].count("kitsune.model.usage") == 2
    terminal = next(item.event for item in pending if item.event.type == "kitsune.run.succeeded")
    assert terminal.type == "kitsune.run.succeeded"
    assert "usage" not in terminal.payload
    await application.outbox.mark_delivered([item.event.event_id for item in pending])
    await application.shutdown()


@pytest.mark.asyncio
async def test_managed_mode_registers_heartbeats_and_delivers_events(tmp_path: Path) -> None:
    """Managed Mode registers, sends best-effort heartbeats, and drains durable events."""

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace)
    add_echo_handler(application)

    async with application:
        await application.execute("echo", {"value": "managed"})
        await asyncio.wait_for(workspace.heartbeat_sent.wait(), timeout=30)
        await asyncio.wait_for(workspace.wait_for_event("kitsune.run.succeeded"), timeout=30)

    assert workspace.registrations[0].descriptor.agent_id == "test-agent"
    assert workspace.begins[0]["source"] == "self"
    assert workspace.begins[0]["input_data"] == {"value": "managed"}
    assert workspace.heartbeats
    assert any(
        event.type == "kitsune.run.succeeded" for batch in workspace.batches for event in batch
    )
    assert workspace.closed


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_inflight_handler(tmp_path: Path) -> None:
    """Shutdown grants an active Handler time to finish before cancellation."""

    application = make_app(tmp_path, shutdown_grace=0.5)
    started = asyncio.Event()
    release = asyncio.Event()

    @application.handler("drain", input_model=Request, output_model=Result)
    async def drain(ctx: RunContext, request: Request) -> Result:
        started.set()
        await release.wait()
        return Result(value=request.value)

    await application.startup()
    task = await application.submit("drain", {"value": "done"})
    await started.wait()
    shutdown = asyncio.create_task(application.shutdown())
    await asyncio.sleep(0)
    release.set()
    await shutdown

    assert Result.model_validate(await task).value == "done"


@pytest.mark.asyncio
async def test_control_api_accepts_run_immediately_and_cancels(tmp_path: Path) -> None:
    """Loopback Standalone Mode can mutate Runs without a configured token."""

    application = make_app(tmp_path)
    started = asyncio.Event()

    @application.handler("wait", input_model=Request, output_model=Result)
    async def wait(ctx: RunContext, request: Request) -> Result:
        started.set()
        await asyncio.Event().wait()
        return Result(value=request.value)

    await application.startup()
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8081") as client:
        run_id = uuid4()
        correlation_id = uuid4()
        assignment = {
            "run_id": str(run_id),
            "agent_id": "test-agent",
            "handler": "wait",
            "source": "on_demand",
            "correlation_id": str(correlation_id),
            "trace_id": "0" * 32,
            "input": {"value": "x"},
        }
        hostile_manifest = await client.get(
            "/_kitsune/manifest", headers={"Host": "attacker.example:8081"}
        )
        cross_origin_manifest = await client.get(
            "/_kitsune/manifest", headers={"Origin": "https://attacker.example"}
        )
        local_manifest = await client.get(
            "/_kitsune/manifest", headers={"Origin": "http://127.0.0.1:8081"}
        )
        hostile_run = await client.post(
            "/_kitsune/runs",
            json=assignment,
            headers={"Host": "attacker.example:8081"},
        )
        cross_origin_run = await client.post(
            "/_kitsune/runs",
            json=assignment,
            headers={"Origin": "https://attacker.example"},
        )
        assert hostile_manifest.status_code == 400
        assert cross_origin_manifest.status_code == 403
        assert local_manifest.status_code == 200
        assert hostile_run.status_code == 400
        assert cross_origin_run.status_code == 403
        response = await client.post(
            "/_kitsune/runs",
            json=assignment,
        )
        assert response.status_code == 202
        await started.wait()
        cross_origin_cancel = await client.post(
            f"/_kitsune/runs/{run_id}/cancel",
            headers={"Origin": "https://attacker.example"},
        )
        assert cross_origin_cancel.status_code == 403
        cancelled = await client.post(f"/_kitsune/runs/{run_id}/cancel")
        assert cancelled.status_code == 202
    await application.shutdown()


@pytest.mark.asyncio
async def test_control_api_requires_correct_bearer_for_run_and_cancel(tmp_path: Path) -> None:
    """Managed Run submission and cancellation reject missing or wrong credentials."""

    application = make_app(tmp_path, agent_token="control-secret")
    started = asyncio.Event()

    @application.handler("wait", input_model=Request, output_model=Result)
    async def wait(ctx: RunContext, request: Request) -> Result:
        started.set()
        await asyncio.Event().wait()
        return Result(value=request.value)

    await application.startup()
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        run_id = uuid4()
        request = {
            "run_id": str(run_id),
            "agent_id": "test-agent",
            "handler": "wait",
            "source": "on_demand",
            "correlation_id": str(uuid4()),
            "trace_id": "0" * 32,
            "input": {"value": "authenticated"},
        }
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
            rejected = await client.post("/_kitsune/runs", json=request, headers=headers)
            assert rejected.status_code == 401
            assert rejected.headers["WWW-Authenticate"] == "Bearer"

        accepted = await client.post(
            "/_kitsune/runs",
            json=request,
            headers={"Authorization": "bEaReR control-secret"},
        )
        assert accepted.status_code == 202
        await started.wait()

        for headers in ({}, {"Authorization": "Bearer wrong"}):
            rejected = await client.post(f"/_kitsune/runs/{run_id}/cancel", headers=headers)
            assert rejected.status_code == 401

        cancelled = await client.post(
            f"/_kitsune/runs/{run_id}/cancel",
            headers={"Authorization": "Bearer control-secret"},
        )
        assert cancelled.status_code == 202
    await application.shutdown()


@pytest.mark.asyncio
async def test_control_manifest_requires_bearer_when_token_is_configured(tmp_path: Path) -> None:
    """A managed Agent does not expose its raw Descriptor without its Control credential."""

    application = make_app(tmp_path, agent_token="control-secret", bind_host="0.0.0.0")
    add_echo_handler(application)
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        missing = await client.get("/_kitsune/manifest")
        wrong = await client.get("/_kitsune/manifest", headers={"Authorization": "Bearer wrong"})
        authorized = await client.get(
            "/_kitsune/manifest",
            headers={"Authorization": "Bearer control-secret"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["agent_id"] == "test-agent"


@pytest.mark.parametrize(
    ("agent_token", "bind_host"),
    [(None, "127.0.0.1"), ("control-secret", "0.0.0.0")],
)
@pytest.mark.asyncio
async def test_control_manifest_preserves_schema_names_without_exposing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_token: str | None,
    bind_host: str,
) -> None:
    """Tokenless loopback and authenticated Descriptor views share safe serialization."""

    sentinel = "descriptor-environment-sentinel"
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)

    class DescriptorRequest(BaseModel):
        password: str = Field(default=sentinel, description=f"credential is {sentinel}")
        safe_field: str = Field(default=sentinel, description=f"default is {sentinel}")

    application = make_app(tmp_path, agent_token=agent_token, bind_host=bind_host)
    application.build_revision = sentinel

    @application.handler(
        "descriptor",
        input_model=DescriptorRequest,
        output_model=Result,
        description=f"handler configured with {sentinel}",
    )
    async def descriptor(ctx: RunContext, request: DescriptorRequest) -> Result:
        return Result(value=request.safe_field)

    headers = {"Authorization": f"Bearer {agent_token}"} if agent_token is not None else {}
    transport = httpx.ASGITransport(app=create_control_api(application))
    base_url = "http://127.0.0.1:8081" if agent_token is None else "http://agent"
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        response = await client.get("/_kitsune/manifest", headers=headers)

    assert response.status_code == 200
    serialized = response.text
    assert sentinel not in serialized
    schema = response.json()["handlers"][0]["input_schema"]
    assert "password" in schema["properties"]
    assert isinstance(schema["properties"]["password"], dict)
    assert schema["properties"]["password"]["default"] == "[REDACTED]"
    assert schema["properties"]["safe_field"]["default"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_control_api_limits_declared_and_streamed_bodies_before_validation(
    tmp_path: Path,
) -> None:
    """Oversized unauthenticated assignments stop outside routing while normal work proceeds."""

    application = make_app(
        tmp_path,
        agent_token="control-secret",
        control_max_request_bytes=1024,
    )
    add_echo_handler(application)
    await application.startup()
    transport = httpx.ASGITransport(app=create_control_api(application))
    yielded_chunks = 0

    async def dishonest_body() -> AsyncIterator[bytes]:
        nonlocal yielded_chunks
        for chunk in (b"{" + b"x" * 699, b"y" * 700, b"must-not-be-consumed"):
            yielded_chunks += 1
            yield chunk

    assignment = {
        "run_id": str(uuid4()),
        "agent_id": "test-agent",
        "handler": "echo",
        "source": "on_demand",
        "correlation_id": str(uuid4()),
        "input": {"value": "bounded"},
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        declared = await client.post(
            "/_kitsune/runs",
            content=b"{}",
            headers={"Content-Length": "2048", "Content-Type": "application/json"},
        )
        streamed = await client.post(
            "/_kitsune/runs",
            content=dishonest_body(),
            headers={"Content-Type": "application/json"},
        )
        accepted = await client.post(
            "/_kitsune/runs",
            json=assignment,
            headers={"Authorization": "Bearer control-secret"},
        )

    assert declared.status_code == 413
    assert streamed.status_code == 413
    assert yielded_chunks == 2
    assert accepted.status_code == 202, accepted.text
    await application.shutdown()


@pytest.mark.asyncio
async def test_control_api_accepts_workspace_agent_run_assignment_wire_contract(
    tmp_path: Path,
) -> None:
    """Resident dispatch accepts the canonical Workspace assignment without field drift."""

    application = make_app(tmp_path, agent_token="control-secret")
    received_contexts: list[RunContext] = []
    completed = asyncio.Event()

    @application.handler("echo", input_model=Request, output_model=Result)
    async def echo(ctx: RunContext, request: Request) -> Result:
        received_contexts.append(ctx)
        completed.set()
        return Result(value=request.value)

    await application.startup()
    run_id = uuid4()
    correlation_id = uuid4()
    assignment = {
        "run_id": str(run_id),
        "agent_id": "test-agent",
        "handler": "echo",
        "source": "on_demand",
        "input": {"value": "workspace"},
        "correlation_id": str(correlation_id),
        "trace_id": "0" * 32,
    }
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        response = await client.post(
            "/_kitsune/runs",
            json=assignment,
            headers={"Authorization": "Bearer control-secret"},
        )
        assert response.status_code == 202, response.text
        await completed.wait()

        wrong_agent = await client.post(
            "/_kitsune/runs",
            json={**assignment, "run_id": str(uuid4()), "agent_id": "other-agent"},
            headers={"Authorization": "Bearer control-secret"},
        )
        assert wrong_agent.status_code == 409

    await application.shutdown()
    context = received_contexts[0]
    assert context.run_id == run_id
    assert context.correlation_id == correlation_id
    assert context.source is RunSource.ON_DEMAND


@pytest.mark.asyncio
async def test_control_api_repeated_completed_run_id_is_idempotent(tmp_path: Path) -> None:
    """A lost 202 response cannot cause a completed resident Run to execute twice."""

    application = make_app(tmp_path, agent_token="control-secret")
    calls = 0
    completed = asyncio.Event()

    @application.handler("echo", input_model=Request, output_model=Result)
    async def echo(ctx: RunContext, request: Request) -> Result:
        nonlocal calls
        calls += 1
        completed.set()
        return Result(value=request.value)

    await application.startup()
    assignment = {
        "run_id": str(uuid4()),
        "agent_id": "test-agent",
        "handler": "echo",
        "source": "on_demand",
        "input": {"value": "once"},
        "correlation_id": str(uuid4()),
    }
    headers = {"Authorization": "Bearer control-secret"}
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        first = await client.post("/_kitsune/runs", json=assignment, headers=headers)
        assert first.status_code == 202, first.text
        await completed.wait()
        for _ in range(10):
            if not application._submitted_runs:
                break
            await asyncio.sleep(0)
        duplicate = await client.post("/_kitsune/runs", json=assignment, headers=headers)

    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["run_id"] == assignment["run_id"]
    assert calls == 1
    await application.shutdown()


@pytest.mark.asyncio
async def test_control_api_rejects_before_202_when_terminal_capacity_is_unavailable(
    tmp_path: Path,
) -> None:
    """Resident dispatch is not accepted unless started and terminal Events are reserved."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "control-full.sqlite3",
        outbox_capacity=2,
        shutdown_grace_seconds=0,
        agent_token=SecretStr("control-secret"),
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=2),
    )
    handler_calls = 0

    @application.handler("echo", input_model=Request, output_model=Result)
    async def echo(ctx: RunContext, request: Request) -> Result:
        nonlocal handler_calls
        handler_calls += 1
        return Result(value=request.value)

    await application.startup()
    run_id = uuid4()
    request = {
        "run_id": str(run_id),
        "agent_id": "test-agent",
        "handler": "echo",
        "source": "on_demand",
        "correlation_id": str(uuid4()),
        "input": {"value": "must-not-run"},
    }
    transport = httpx.ASGITransport(app=create_control_api(application))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        response = await client.post(
            "/_kitsune/runs",
            json=request,
            headers={"Authorization": "Bearer control-secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Agent Event Outbox cannot durably admit this Run"
    assert handler_calls == 0
    with sqlite3.connect(settings.outbox_path) as connection:
        queued = [
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM event_outbox")
        ]
    assert all(payload.get("run_id") != str(run_id) for payload in queued)
    with pytest.raises(OutboxFullError):
        await application.shutdown()


def test_control_api_without_token_rejects_non_loopback_bind(tmp_path: Path) -> None:
    """An unauthenticated Control API cannot be exposed beyond the loopback interface."""

    application = make_app(tmp_path, bind_host="0.0.0.0")

    with pytest.raises(ValueError, match="KITSUNE_AGENT_TOKEN"):
        create_control_api(application)


def test_control_api_rejects_bypassed_empty_token_configuration(tmp_path: Path) -> None:
    """Construction remains fail-closed if a caller bypasses settings validation."""

    application = make_app(tmp_path, agent_token="valid", bind_host="0.0.0.0")
    application.settings = application.settings.model_copy(update={"agent_token": SecretStr(" \t")})

    with pytest.raises(ValueError, match="non-empty"):
        create_control_api(application)


def test_cli_once_auto_detects_managed_ephemeral_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Managed ``agent once`` executes its assignment without resident registration."""

    parent = uuid4()
    correlation = uuid4()
    run_id = uuid4()
    workspace = FakeWorkspaceClient(
        {
            "input": {"value": "ephemeral"},
            "source": "child",
            "parent_run_id": str(parent),
            "correlation_id": str(correlation),
            "deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }
    )
    application = make_app(tmp_path, workspace=workspace)
    capture = CapturePlugin()
    application.use(capture)
    add_echo_handler(application)
    module = ModuleType("kitsune_test_ephemeral")
    module.__dict__["application"] = application
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("KITSUNE_RUN_ID", str(run_id))
    monkeypatch.setenv("KITSUNE_HANDLER", "echo")
    environment_deadline = datetime.now(UTC) + timedelta(seconds=30)
    monkeypatch.setenv("KITSUNE_RUN_DEADLINE", environment_deadline.isoformat())

    result = CliRunner().invoke(cli_app, ["agent", "once", "kitsune_test_ephemeral:application"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output.splitlines()[-1]) == {"value": "ephemeral"}
    run_context = next(ctx for ctx in capture.contexts if ctx.run_id == run_id)
    assert run_context.source is RunSource.CHILD
    assert run_context.parent_run_id == parent
    assert run_context.correlation_id == correlation
    assert run_context.deadline == environment_deadline
    assert workspace.acks == [(run_id, application.runtime_instance_id)]
    assert len(workspace.registrations) == 1
    assert workspace.registrations[0].control_url is None
    assert workspace.heartbeats == []
    assert workspace.begins == []


def test_cli_once_reports_handler_failure_without_exception_text_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot failures expose only a stable exception classification at process exit."""

    secret = "cli-failure-sentinel"
    application = make_app(tmp_path)

    @application.handler("fail", input_model=Request, output_model=Result)
    async def fail(ctx: RunContext, request: Request) -> Result:
        del ctx, request
        raise RuntimeError(f"Authorization: Bearer {secret}")

    module = ModuleType("kitsune_test_cli_failure")
    module.__dict__["application"] = application
    monkeypatch.setitem(sys.modules, module.__name__, module)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value":"ordinary"}', encoding="utf-8")

    result = CliRunner().invoke(
        cli_app,
        [
            "agent",
            "once",
            "kitsune_test_cli_failure:application",
            "--handler",
            "fail",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1
    assert result.output.strip() == "Agent execution failed (RuntimeError)"
    assert secret not in result.output
    assert "Traceback" not in result.output
    assert secret not in repr(result.exception)


@pytest.mark.asyncio
async def test_managed_ephemeral_outbox_survives_failed_exit_and_new_recovery_process(
    tmp_path: Path,
) -> None:
    """A failed bounded exit leaves a reopenable queue for Workspace-owned recovery."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        def __init__(self) -> None:
            super().__init__()
            self.delivery_attempted = asyncio.Event()

        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            self.delivery_attempted.set()
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    application = make_app(tmp_path, workspace=workspace, shutdown_grace=0.05)
    add_echo_handler(application)
    await application.startup(mode=RuntimeMode.EPHEMERAL)
    await workspace.delivery_attempted.wait()
    await application.execute("echo", {"value": "ephemeral"}, source=RunSource.ON_DEMAND)

    with pytest.raises(EphemeralDeliveryIncompleteError) as raised:
        await application.shutdown()

    assert application.outbox is not None
    queued_after_exit = await application.outbox.size()
    assert raised.value.remaining == queued_after_exit
    assert queued_after_exit > 0
    assert workspace.closed
    assert len(workspace.registrations) == 1
    assert workspace.heartbeats == []

    recovery_workspace = FakeWorkspaceClient()
    recovery_application = make_app(tmp_path, workspace=recovery_workspace, shutdown_grace=5)
    recovery = await recovery_application.drain_outbox_only()

    assert recovery.remaining == 0
    assert recovery_application.outbox is not None
    assert await recovery_application.outbox.size() == 0
    assert recovery_workspace.registrations == []
    assert recovery_workspace.acks == []
    assert recovery_workspace.begins == []
    assert any(
        event.type == "kitsune.run.succeeded"
        for batch in recovery_workspace.batches
        for event in batch
    )


def test_cli_once_registers_externally_launched_managed_ephemeral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed one-shot without an assignment registers before beginning its own Run."""

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace)
    add_echo_handler(application)
    module = ModuleType("kitsune_test_external_ephemeral")
    module.__dict__["application"] = application
    monkeypatch.setitem(sys.modules, module.__name__, module)
    input_path = tmp_path / "external-input.json"
    input_path.write_text('{"value":"external"}', encoding="utf-8")

    result = CliRunner().invoke(
        cli_app,
        [
            "agent",
            "once",
            "kitsune_test_external_ephemeral:application",
            "--handler",
            "echo",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(workspace.registrations) == 1
    assert workspace.registrations[0].runtime_instance_id == application.runtime_instance_id
    assert workspace.registrations[0].control_url is None
    assert workspace.heartbeats == []
    assert len(workspace.begins) == 1
    assert workspace.begins[0]["source"] is RunSource.SELF
    assert workspace.begins[0]["runtime_instance_id"] == application.runtime_instance_id


def test_cli_external_ephemeral_recovers_exact_persisted_outbox_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new drain-only process replays the failed process queue without Agent work."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace unavailable")

    outbox_path = tmp_path / "persistent" / "events.sqlite3"
    input_path = tmp_path / "external-input.json"
    input_path.write_text('{"value":"external"}', encoding="utf-8")
    monkeypatch.setenv("KITSUNE_OUTBOX_PATH", str(outbox_path))
    monkeypatch.delenv("KITSUNE_OUTBOX_DRAIN_ONLY", raising=False)
    monkeypatch.delenv("KITSUNE_RUN_ID", raising=False)
    settings = KitsuneSettings(
        workspace_url=None,
        agent_token=None,
        shutdown_grace_seconds=0.05,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.02,
    )
    assert settings.outbox_path == outbox_path
    unavailable_workspace = UnavailableWorkspace()
    failed_application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=unavailable_workspace,
    )
    handler_calls = 0

    @failed_application.handler("echo", input_model=Request, output_model=Result)
    async def echo(ctx: RunContext, request: Request) -> Result:
        nonlocal handler_calls
        handler_calls += 1
        ctx.raise_if_cancelled()
        return Result(value=request.value)

    failed_module = ModuleType("kitsune_test_failed_external_ephemeral")
    failed_module.__dict__["application"] = failed_application
    monkeypatch.setitem(sys.modules, failed_module.__name__, failed_module)

    failed = CliRunner().invoke(
        cli_app,
        [
            "agent",
            "once",
            "kitsune_test_failed_external_ephemeral:application",
            "--handler",
            "echo",
            "--input",
            str(input_path),
        ],
    )

    assert failed.exit_code == EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE
    assert "ephemeral event delivery incomplete" in failed.output
    assert handler_calls == 1
    assert len(unavailable_workspace.registrations) == 1
    assert len(unavailable_workspace.begins) == 1
    assert unavailable_workspace.heartbeats == []
    with sqlite3.connect(outbox_path) as connection:
        persisted_rows = connection.execute(
            "SELECT event_id, payload FROM event_outbox ORDER BY created_at, event_id"
        ).fetchall()
    persisted_event_ids = [UUID(row[0]) for row in persisted_rows]
    persisted_event_types = {json.loads(row[1])["type"] for row in persisted_rows}
    assert persisted_event_ids
    assert persisted_event_types == {
        "kitsune.agent.started",
        "kitsune.agent.stopped",
        "kitsune.agent.stopping",
        "kitsune.run.started",
        "kitsune.run.succeeded",
    }

    recovery_workspace = FakeWorkspaceClient()
    recovery_settings = KitsuneSettings(
        workspace_url=None,
        agent_token=None,
        shutdown_grace_seconds=1,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.02,
    )
    assert recovery_settings.outbox_path == outbox_path
    recovery_application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=recovery_settings,
        workspace_client=recovery_workspace,
    )
    recovery_module = ModuleType("kitsune_test_recovered_external_ephemeral")
    recovery_module.__dict__["application"] = recovery_application
    monkeypatch.setitem(sys.modules, recovery_module.__name__, recovery_module)
    monkeypatch.setenv("KITSUNE_OUTBOX_DRAIN_ONLY", "1")

    recovered = CliRunner().invoke(
        cli_app,
        ["agent", "once", "kitsune_test_recovered_external_ephemeral:application"],
    )

    assert recovered.exit_code == 0, recovered.output
    delivered_event_ids = [
        event.event_id for batch in recovery_workspace.batches for event in batch
    ]
    assert delivered_event_ids == persisted_event_ids
    assert handler_calls == 1
    assert recovery_workspace.registrations == []
    assert recovery_workspace.heartbeats == []
    assert recovery_workspace.acks == []
    assert recovery_workspace.begins == []
    with sqlite3.connect(outbox_path) as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
    assert remaining == 0


def test_cli_internal_drain_only_has_no_agent_lifecycle_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace recovery drains a mounted queue without running or acknowledging a Run."""

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace)
    assert application.outbox is not None
    event = KitsuneEvent(
        type="kitsune.run.succeeded",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
        runtime_instance_id=application.runtime_instance_id,
        run_id=uuid4(),
        correlation_id=uuid4(),
    )
    asyncio.run(application.outbox.enqueue(event))
    module = ModuleType("kitsune_test_drain_only")
    module.__dict__["application"] = application
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("KITSUNE_OUTBOX_DRAIN_ONLY", "1")

    result = CliRunner().invoke(cli_app, ["agent", "once", "kitsune_test_drain_only:application"])

    assert result.exit_code == 0, result.output
    assert workspace.registrations == []
    assert workspace.heartbeats == []
    assert workspace.acks == []
    assert workspace.begins == []
    assert [item.event_id for batch in workspace.batches for item in batch] == [event.event_id]


def test_cli_internal_drain_only_uses_reserved_incomplete_delivery_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-unavailable recovery process exits with the reserved temporary-failure code."""

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace, shutdown_grace=0)
    assert application.outbox is not None
    event = KitsuneEvent(
        type="kitsune.run.succeeded",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
        runtime_instance_id=application.runtime_instance_id,
    )
    asyncio.run(application.outbox.enqueue(event))
    module = ModuleType("kitsune_test_drain_incomplete")
    module.__dict__["application"] = application
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("KITSUNE_OUTBOX_DRAIN_ONLY", "true")

    result = CliRunner().invoke(
        cli_app,
        ["agent", "once", "kitsune_test_drain_incomplete:application"],
    )

    assert result.exit_code == EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE
    assert "ephemeral event delivery incomplete" in result.output
    assert workspace.registrations == []
    assert workspace.acks == []
    assert workspace.begins == []


@pytest.mark.asyncio
async def test_workspace_client_begin_and_ack_wire_contract() -> None:
    """Agent Run persistence sends lineage, input, header, and explicit acknowledgement."""

    run_id = uuid4()
    runtime_id = uuid4()
    parent_id = uuid4()
    correlation_id = uuid4()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "run_id": str(run_id),
                    "agent_id": "test-agent",
                    "handler": "self-handler",
                    "source": "child",
                    "input": {"value": "persisted"},
                    "parent_run_id": str(parent_id),
                    "correlation_id": str(correlation_id),
                    "trace_id": "0" * 32,
                    "deadline": "2030-01-01T00:00:00Z",
                },
            )
        return httpx.Response(200, json={"run_id": str(run_id), "status": "running"})

    client = WorkspaceClient(
        "https://workspace.invalid",
        "agent-token",
        transport=httpx.MockTransport(handler),
    )
    await client.begin_run(
        run_id=run_id,
        agent_id="test-agent",
        runtime_instance_id=runtime_id,
        handler="self-handler",
        source=RunSource.CHILD,
        parent_run_id=parent_id,
        correlation_id=correlation_id,
        trace_id="0" * 32,
        input_data={"value": "persisted"},
        timeout_seconds=90,
        idempotency_key="self-run",
    )
    assignment = await client.get_run_assignment(run_id)
    await client.acknowledge_run(run_id, runtime_instance_id=runtime_id)
    await client.close()

    begin_body = json.loads(requests[0].content)
    assert requests[0].headers["X-Kitsune-Agent-ID"] == "test-agent"
    assert begin_body["source"] == "child"
    assert begin_body["runtime_instance_id"] == str(runtime_id)
    assert begin_body["parent_run_id"] == str(parent_id)
    assert begin_body["input"] == {"value": "persisted"}
    assert begin_body["idempotency_key"] == "self-run"
    assert assignment.run_id == run_id
    assert assignment.source is RunSource.CHILD
    assert assignment.deadline == datetime(2030, 1, 1, tzinfo=UTC)
    assert json.loads(requests[2].content) == {
        "status": "running",
        "runtime_instance_id": str(runtime_id),
    }


@pytest.mark.asyncio
async def test_failed_self_begin_never_replays_events_for_an_unknown_run(tmp_path: Path) -> None:
    """Workspace recovery receives no Run Event when begin never persisted that Run."""

    class BeginOutageWorkspace(FakeWorkspaceClient):
        async def begin_run(self, **values: Any) -> dict[str, Any]:
            self.begins.append(values)
            raise httpx.ConnectError("workspace begin unavailable")

        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            del events
            raise httpx.ConnectError("workspace events unavailable")

    run_id = uuid4()
    workspace = BeginOutageWorkspace()
    application = make_app(tmp_path, workspace=workspace, shutdown_grace=0)
    handler_calls = 0

    @application.handler("never", input_model=Request, output_model=Result)
    async def never(ctx: RunContext, request: Request) -> Result:
        nonlocal handler_calls
        handler_calls += 1
        return Result(value=request.value)

    await application.startup()
    with pytest.raises(httpx.ConnectError, match="begin unavailable"):
        await application.execute("never", {"value": "blocked"}, run_id=run_id)
    await application.shutdown()

    assert handler_calls == 0
    assert len(workspace.begins) == 1
    assert application.outbox is not None
    with sqlite3.connect(application.outbox.path) as connection:
        queued = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM event_outbox ORDER BY created_at, event_id"
            )
        ]
    assert all(payload.get("run_id") != str(run_id) for payload in queued)

    recovery_workspace = FakeWorkspaceClient()
    recovery_application = make_app(
        tmp_path,
        workspace=recovery_workspace,
        shutdown_grace=1,
    )
    await recovery_application.drain_outbox_only()

    recovered = [event for batch in recovery_workspace.batches for event in batch]
    assert recovered
    assert all(event.run_id != run_id for event in recovered)
    assert all(event.type != "kitsune.event.rejected" for event in recovered)


@pytest.mark.asyncio
async def test_managed_child_run_is_persisted_before_child_events(tmp_path: Path) -> None:
    """SDK-managed child Run calls Workspace begin with inherited lineage."""

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace)

    @application.handler("parent", input_model=Request, output_model=Result)
    async def parent(ctx: RunContext, request: Request) -> Result:
        async with ctx.child_run(name="remote-child", metadata={"kind": "delegate"}):
            return Result(value=request.value)

    async with application:
        await application.execute("parent", {"value": "child"})
        await asyncio.sleep(0.02)

    assert [begin["source"] for begin in workspace.begins] == ["self", "child"]
    assert workspace.begins[1]["parent_run_id"] == workspace.begins[0]["run_id"]
    assert {begin["runtime_instance_id"] for begin in workspace.begins} == {
        application.runtime_instance_id
    }
    assert workspace.begins[1]["correlation_id"] == workspace.begins[0]["correlation_id"]
    assert workspace.begins[1]["handler"] == "remote-child"


@pytest.mark.asyncio
async def test_persisted_child_start_failure_emits_terminal_event(tmp_path: Path) -> None:
    """A child persisted before a critical start hook failure still becomes terminal."""

    class ChildStartFailurePlugin(CapturePlugin):
        async def on_run_started(self, ctx: RunContext) -> None:
            if ctx.source is RunSource.CHILD:
                raise RuntimeError("child start observation failed")
            await super().on_run_started(ctx)

    workspace = FakeWorkspaceClient()
    application = make_app(tmp_path, workspace=workspace)
    application.use(ChildStartFailurePlugin("child-start", critical=True))

    @application.handler("parent", input_model=Request, output_model=Result)
    async def parent(ctx: RunContext, request: Request) -> Result:
        async with ctx.child_run(name="persisted-child"):
            return Result(value=request.value)

    async with application:
        with pytest.raises(RuntimeError, match="critical plugin 'child-start'"):
            await application.execute("parent", {"value": "child"})
        child_run_id = workspace.begins[1]["run_id"]
        await asyncio.wait_for(
            workspace.wait_for_event("kitsune.run.failed", run_id=child_run_id), timeout=30
        )

    child_events = [
        event for batch in workspace.batches for event in batch if event.run_id == child_run_id
    ]
    assert any(event.type == "kitsune.run.failed" for event in child_events)


@pytest.mark.asyncio
async def test_shutdown_retries_transient_failure_until_outbox_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ephemeral shutdown retains terminal events through a transient Workspace outage."""

    logical_time = 0.0

    def monotonic_clock() -> float:
        nonlocal logical_time
        logical_time += 0.001
        return logical_time

    monkeypatch.setattr("kitsune.app.monotonic", monotonic_clock)
    monkeypatch.setattr("kitsune.outbox.monotonic", monotonic_clock)

    class TransientWorkspace(FakeWorkspaceClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0
            self.first_failure = asyncio.Event()
            self.background_retry_started = asyncio.Event()

        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            self.attempts += 1
            if self.attempts == 1:
                self.first_failure.set()
                raise httpx.ConnectError("temporary outage")
            if self.attempts == 2:
                self.background_retry_started.set()
                await asyncio.Event().wait()
            await super().send_events(events)

    workspace = TransientWorkspace()
    application = make_app(tmp_path, workspace=workspace, shutdown_grace=1)
    add_echo_handler(application)
    await application.startup()
    await workspace.first_failure.wait()
    await application.execute("echo", {"value": "ephemeral"}, source=RunSource.ON_DEMAND)
    await workspace.background_retry_started.wait()
    await application.shutdown()

    assert application.outbox is not None
    assert await application.outbox.size() == 0
    assert workspace.attempts >= 3
    assert any(
        event.type == "kitsune.run.succeeded" for batch in workspace.batches for event in batch
    )


@pytest.mark.asyncio
async def test_shutdown_cleans_resources_when_lifecycle_event_hits_full_outbox(
    tmp_path: Path,
) -> None:
    """A full durable queue is reported only after Plugin, task, client, and state cleanup."""

    class UnavailableWorkspace(FakeWorkspaceClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_failure = asyncio.Event()

        async def send_events(self, events: Sequence[KitsuneEvent]) -> None:
            self.first_failure.set()
            raise httpx.ConnectError("workspace unavailable")

    workspace = UnavailableWorkspace()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "full.sqlite3",
        outbox_capacity=1,
        shutdown_grace_seconds=0.05,
        heartbeat_interval_seconds=0.01,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.01,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path, capacity=1),
    )
    capture = CapturePlugin()
    application.use(capture)
    await application.startup()
    await workspace.first_failure.wait()

    with pytest.raises(OutboxFullError):
        await application.shutdown()

    assert capture.lifecycle[-1] == "stop:test-agent"
    assert workspace.closed
    assert not application.started
    assert not application._stopping  # pyright: ignore[reportPrivateUsage]
    assert not application._background_tasks  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_high_retry_attempt_saturates_backoff_without_exponentiation_overflow(
    tmp_path: Path,
) -> None:
    """A prolonged outage caps retry arithmetic before computing a huge power."""

    outbox = EventOutbox(tmp_path / "high-attempt.sqlite3")
    event = KitsuneEvent(
        type="kitsune.run.started",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    await outbox.enqueue(event)
    with sqlite3.connect(outbox.path) as connection:
        connection.execute(
            "UPDATE event_outbox SET attempts = ? WHERE event_id = ?",
            (1_000_000, str(event.event_id)),
        )

    await outbox.mark_failed(
        [event.event_id],
        error="still unavailable",
        initial_backoff=0.001,
        maximum_backoff=2,
    )

    delay = await outbox.seconds_until_next_attempt()
    assert delay is not None and 0 <= delay <= 2.1


@pytest.mark.asyncio
async def test_oversized_progress_is_bounded_before_following_terminal_delivery(
    tmp_path: Path,
) -> None:
    """An oversized custom Event becomes a marker and cannot poison the terminal Event."""

    workspace = FakeWorkspaceClient()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "bounded-events.sqlite3",
        event_max_payload_bytes=1024,
        event_max_batch_bytes=2048,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.02,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path),
    )
    add_echo_handler(application)

    async with application:
        marker = await application.emit(
            "kitsune.custom.progress",
            payload={"blob": "x" * 10_000},
        )
        result = await application.execute("echo", {"value": "done"})
        assert result == Result(value="done")
        await workspace.wait_for_event("kitsune.run.succeeded")

    delivered = [event for batch in workspace.batches for event in batch]
    rejected = next(event for event in delivered if event.event_id == marker.event_id)
    assert rejected.type == "kitsune.event.rejected"
    assert rejected.payload["rejected_event_type"] == "kitsune.custom.progress"
    assert "blob" not in rejected.payload
    assert any(event.type == "kitsune.run.succeeded" for event in delivered)


@pytest.mark.asyncio
async def test_oversized_handler_output_becomes_typed_bounded_failure_event(
    tmp_path: Path,
) -> None:
    """Raw oversized output never enters SQLite and terminal state remains deliverable."""

    workspace = FakeWorkspaceClient()
    settings = KitsuneSettings(
        outbox_path=tmp_path / "bounded-output.sqlite3",
        event_max_payload_bytes=1024,
        event_max_batch_bytes=2048,
        event_retry_initial_seconds=0.01,
        event_retry_max_seconds=0.02,
    )
    application = KitsuneApp(
        agent_id="test-agent",
        version="1.0.0",
        settings=settings,
        workspace_client=workspace,
        outbox=EventOutbox(settings.outbox_path),
    )

    @application.handler("large", input_model=Request, output_model=Result)
    async def large(ctx: RunContext, request: Request) -> Result:
        return Result(value="x" * 10_000)

    async with application:
        result = await application.execute("large", {"value": "ignored"})
        assert len(Result.model_validate(result).value) == 10_000
        await workspace.wait_for_event("kitsune.run.failed")

    terminal = next(
        event
        for batch in workspace.batches
        for event in batch
        if event.type == "kitsune.run.failed"
    )
    assert terminal.payload["error"]["type"] == "OutputTooLarge"
    assert "output" not in terminal.payload
    assert "x" * 100 not in terminal.model_dump_json()


@pytest.mark.asyncio
async def test_nonretryable_poison_event_is_quarantined_without_blocking_terminal(
    tmp_path: Path,
) -> None:
    """A 422 singleton is replaced once while a valid terminal Event is acknowledged."""

    outbox = EventOutbox(tmp_path / "poison.sqlite3")
    poison = KitsuneEvent(
        type="kitsune.custom.poison",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    terminal = KitsuneEvent(
        type="kitsune.run.succeeded",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    await outbox.enqueue(poison)
    await outbox.enqueue(terminal)
    accepted: list[KitsuneEvent] = []

    async def sender(events: Sequence[KitsuneEvent]) -> None:
        if any(event.type == poison.type for event in events):
            raise NonRetryableEventDeliveryError(status_code=422, detail="invalid event")
        accepted.extend(events)

    delivered = await outbox.flush_once(
        sender,
        batch_size=100,
        initial_backoff=0.01,
        maximum_backoff=0.02,
    )
    await outbox.flush_once(
        sender,
        batch_size=100,
        initial_backoff=0.01,
        maximum_backoff=0.02,
    )

    assert delivered == 1
    assert [event.type for event in accepted] == [
        "kitsune.run.succeeded",
        "kitsune.event.rejected",
    ]
    assert await outbox.size() == 0


@pytest.mark.asyncio
async def test_workspace_retry_after_is_clamped_to_configured_retry_maximum(
    tmp_path: Path,
) -> None:
    """A server retry hint cannot exceed the SDK's configured durable retry bound."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"detail": "Agent retained Event count quota exceeded"},
            headers={"Retry-After": "60"},
        )

    client = WorkspaceClient(
        "https://workspace.invalid",
        "agent-token",
        transport=httpx.MockTransport(handler),
    )
    outbox_directory = tmp_path / "retry-after"
    outbox_directory.mkdir(mode=0o700)
    outbox = EventOutbox(outbox_directory / "events.sqlite3")
    event = KitsuneEvent(
        type="kitsune.custom.progress",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    await outbox.enqueue(event)

    with pytest.raises(RetryableEventDeliveryError) as captured:
        await outbox.flush_once(
            client.send_events,
            batch_size=100,
            initial_backoff=0.01,
            maximum_backoff=0.02,
        )
    await client.close()

    assert captured.value.retry_after_seconds == 60
    delay = await outbox.seconds_until_next_attempt()
    assert delay is not None and 0 <= delay <= 0.2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "expected_seconds"),
    [("999999", 3_600), ("not-a-number", 1)],
)
async def test_workspace_retry_after_is_parsed_with_safe_bounds(
    retry_after: str,
    expected_seconds: int,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": retry_after})

    client = WorkspaceClient(
        "https://workspace.invalid",
        "agent-token",
        transport=httpx.MockTransport(handler),
    )
    event = KitsuneEvent(
        type="kitsune.custom.progress",
        occurred_at=datetime.now(UTC),
        agent_id="test-agent",
    )
    try:
        with pytest.raises(RetryableEventDeliveryError) as captured:
            await client.send_events([event])
    finally:
        await client.close()

    assert captured.value.retry_after_seconds == expected_seconds


@pytest.mark.asyncio
async def test_workspace_client_splits_event_batches_under_serialized_byte_cap() -> None:
    """Batch count never allows the serialized HTTP request to exceed its configured cap."""

    request_sizes: list[int] = []
    received_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_sizes.append(len(request.content))
        received_ids.extend(item["event_id"] for item in json.loads(request.content)["events"])
        return httpx.Response(202, json={"accepted": 1, "duplicates": []})

    events = [
        KitsuneEvent(
            type="kitsune.custom.progress",
            occurred_at=datetime.now(UTC),
            agent_id="test-agent",
            payload={"value": str(index) * 300},
        )
        for index in range(3)
    ]
    client = WorkspaceClient(
        "https://workspace.invalid",
        "agent-token",
        max_event_batch_bytes=900,
        transport=httpx.MockTransport(handler),
    )

    await client.send_events(events)
    await client.close()

    assert len(request_sizes) > 1
    assert all(size <= 900 for size in request_sizes)
    assert received_ids == [str(event.event_id) for event in events]
