"""Regression coverage for scheduler overlap and dispatch state publication."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import ManifestFactory, settings_for
from kitsune import EventOutbox
from kitsune_contracts import KitsuneEvent
from sqlalchemy import select

from kitsune_workspace import runtime as runtime_module
from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import Handler, Run, RuntimeInstance, Schedule, UsageRecord
from kitsune_workspace.util import utcnow


def _control(root: Path, manifest_factory: ManifestFactory, **manifest: Any) -> ControlPlane:
    manifest_factory(**manifest)
    settings = settings_for(root)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    return control


async def _close(control: ControlPlane) -> None:
    await control.runtime.shutdown()
    control.telemetry.shutdown()
    control.database.dispose()


@pytest.mark.asyncio
async def test_monitor_cancellation_drains_probe_children(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="external", mode="resident")
    runtime_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=runtime_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                started_at=utcnow(),
                ready_at=utcnow(),
                control_url="https://agent.example.invalid",
                runtime_metadata={},
            )
        )

    probe_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    finished: set[str] = set()
    children: set[asyncio.Task[Any]] = set()

    async def blocked_probe(*_: Any) -> None:
        task = asyncio.current_task()
        assert task is not None
        children.add(task)
        probe_started.set()
        try:
            await release.wait()
        finally:
            finished.add("probe")
            children.remove(task)

    async def blocked_cleanup() -> int:
        task = asyncio.current_task()
        assert task is not None
        children.add(task)
        cleanup_started.set()
        try:
            await release.wait()
        finally:
            finished.add("cleanup")
            children.remove(task)
        return 0

    monkeypatch.setattr(control.runtime, "_monitor_instance", blocked_probe)
    monkeypatch.setattr(control.runtime, "cleanup_terminal_containers", blocked_cleanup)
    monitor_task = asyncio.create_task(control.runtime.monitor())
    try:
        await asyncio.gather(probe_started.wait(), cleanup_started.wait())
        monitor_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor_task
        assert finished == {"probe", "cleanup"}
        assert not children
    finally:
        release.set()
        if not monitor_task.done():
            monitor_task.cancel()
            await monitor_task
        await _close(control)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overlap", "created", "dispatched", "old_status", "new_status"),
    [
        ("allow", 1, 1, "running", "running"),
        ("skip", 0, 0, "running", None),
        ("queue", 1, 0, "running", "queued"),
        ("replace", 1, 1, "cancelled", "running"),
    ],
)
async def test_schedule_overlap_policy_controls_dispatch(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
    created: int,
    dispatched: int,
    old_status: str,
    new_status: str | None,
) -> None:
    trigger = f"""    - id: periodic
      type: schedule
      handler: default
      cron: "* * * * *"
      timezone: UTC
      overlap: {overlap}
      misfire_grace_seconds: 300
"""
    control = _control(
        tmp_path,
        manifest_factory,
        max_concurrency=3,
        queue_capacity=5,
        triggers=trigger,
    )
    runtime_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=runtime_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                started_at=utcnow(),
                ready_at=utcnow(),
                control_url="https://agent.example.invalid",
                runtime_metadata={},
            )
        )
    active, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="schedule",
        trigger_id="periodic",
        input_value={},
        start_running=True,
    )
    with control.database.session() as session:
        schedule = session.scalar(select(Schedule))
        assert schedule is not None
        schedule.next_fire_at = utcnow()

    async def dispatch_resident(*_: Any, **__: Any) -> None:
        return None

    async def cancel(run_id: str, **_: Any) -> Run:
        with control.database.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.status = "cancelled"
            run.ended_at = utcnow()
            return run

    monkeypatch.setattr(control.runtime, "dispatch_resident", dispatch_resident)
    monkeypatch.setattr(control.runs, "cancel", cancel)
    try:
        assert await control.dispatch_schedules() == created
        assert await control.runs.dispatch_available() == dispatched
        with control.database.session() as session:
            runs = list(session.scalars(select(Run).order_by(Run.created_at)))
            assert runs[0].run_id == active.run_id
            assert runs[0].status == old_status
            if new_status is None:
                assert len(runs) == 1
            else:
                assert len(runs) == 2
                assert runs[1].status == new_status
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_ephemeral_dispatch_publishes_persisted_dispatching_state(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(
        tmp_path,
        manifest_factory,
        adapter="process",
        mode="ephemeral",
    )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
    )
    published: list[tuple[str, dict[str, Any]]] = []

    async def start(*_: Any, **__: Any) -> dict[str, Any]:
        with control.database.session() as session:
            assigned = session.get(Run, run.run_id)
            assert assigned is not None
            assert assigned.runtime_instance_id is not None
        return {"pid": 12345}

    async def publish(event_type: str, data: dict[str, Any]) -> None:
        published.append((event_type, data))

    monkeypatch.setattr(control.runtime.process, "start", start)
    monkeypatch.setattr(control.runtime, "publish", publish)
    try:
        assert await control.runs.dispatch_available() == 1
        with control.database.session() as session:
            stored = session.get(Run, run.run_id)
            assert stored is not None
            assert stored.status == "dispatching"
            assert stored.runtime_instance_id is not None
        run_updates = [data for event_type, data in published if event_type == "run"]
        assert run_updates == [{"run_id": run.run_id, "status": "dispatching"}]
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_docker_outbox_survives_exit_and_is_recovered_before_cleanup(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(
        tmp_path,
        manifest_factory,
        adapter="docker",
        mode="ephemeral",
    )
    instance_id = str(uuid.uuid4())
    outbox_path, _ = control.runtime._outbox_paths("demo-agent", instance_id, "docker")
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="docker",
                mode="ephemeral",
                status="ready",
                container_id="container-outbox",
                started_at=utcnow(),
                ready_at=utcnow(),
                runtime_metadata={"outbox_path": str(outbox_path)},
            )
        )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
        start_running=True,
    )
    with control.database.session() as session:
        instance = session.get(RuntimeInstance, instance_id)
        assert instance is not None
        metadata = dict(instance.runtime_metadata)
        metadata["ephemeral_run_id"] = run.run_id
        instance.runtime_metadata = metadata
    container_outbox_path = tmp_path / "container-layer" / "events.sqlite3"
    outbox = EventOutbox(container_outbox_path)
    usage_event = KitsuneEvent.model_validate(
        {
            "event_id": str(uuid.uuid4()),
            "type": "kitsune.model.usage",
            "occurred_at": utcnow().isoformat(),
            "agent_id": "demo-agent",
            "runtime_instance_id": instance_id,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "severity": "info",
            "payload": {
                "provider": "test",
                "model": "fake",
                "request_count": 1,
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        }
    )
    terminal_event = KitsuneEvent.model_validate(
        {
            "event_id": str(uuid.uuid4()),
            "type": "kitsune.run.succeeded",
            "occurred_at": utcnow().isoformat(),
            "agent_id": "demo-agent",
            "runtime_instance_id": instance_id,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "severity": "info",
            "payload": {"output": {"recovered": True}},
        }
    )
    assert await outbox.enqueue(usage_event)
    assert await outbox.enqueue(terminal_event)
    removed: list[str] = []

    async def inspect(_: str) -> dict[str, Any]:
        return {"State": {"Running": False, "ExitCode": 75}}

    async def archive_and_remove(container_id: str) -> bool:
        removed.append(container_id)
        return True

    async def export_outbox(_: str, destination: Path) -> bool:
        def copy() -> bool:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{container_outbox_path}{suffix}")
                if source.exists():
                    Path(f"{destination}{suffix}").write_bytes(source.read_bytes())
            return destination.exists()

        return await asyncio.to_thread(copy)

    def unavailable(_: str, __: Path) -> int:
        raise OSError("Workspace event persistence unavailable")

    monkeypatch.setattr(control.runtime.docker, "inspect", inspect)
    monkeypatch.setattr(control.runtime.docker, "export_outbox", export_outbox)
    monkeypatch.setattr(control.runtime.docker, "archive_and_remove", archive_and_remove)
    control.runtime.set_outbox_recovery(unavailable)
    try:
        await control.runtime.monitor()
        with control.database.session() as session:
            stored_run = session.get(Run, run.run_id)
            stored_instance = session.get(RuntimeInstance, instance_id)
            assert stored_run is not None and stored_run.status == "running"
            assert stored_instance is not None and stored_instance.status == "unhealthy"
        assert outbox_path.exists()
        assert removed == []

        control.runtime.set_outbox_recovery(control.events_service.recover_outbox)
        await control.runtime.monitor()
        with control.database.session() as session:
            stored_run = session.get(Run, run.run_id)
            stored_instance = session.get(RuntimeInstance, instance_id)
            assert stored_run is not None and stored_run.status == "succeeded"
            assert stored_run.output == {"recovered": True}
            assert stored_instance is not None and stored_instance.status == "failed"
            assert stored_instance.runtime_metadata["outbox_recovered_events"] == 2
            usage = session.query(UsageRecord).one()
            assert usage.total_tokens == 5
        assert removed == ["container-outbox"]
        assert not outbox_path.exists()
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_resident_assignment_is_persisted_before_fast_terminal_events(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="external", mode="resident")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(Handler(agent_id="demo-agent", name="default", default_timeout_seconds=30))
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                control_url="https://agent.example.invalid",
                started_at=utcnow(),
                ready_at=utcnow(),
            )
        )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
    )

    async def terminal_before_acceptance(*_: Any, **__: Any) -> None:
        with control.database.session() as session:
            assigned = session.get(Run, run.run_id)
            assert assigned is not None
            assert assigned.status == "dispatching"
            assert assigned.runtime_instance_id == instance_id
        events = [
            {
                "event_id": str(uuid.uuid4()),
                "type": "kitsune.run.started",
                "occurred_at": utcnow().isoformat(),
                "agent_id": "demo-agent",
                "runtime_instance_id": instance_id,
                "run_id": run.run_id,
                "correlation_id": run.correlation_id,
                "severity": "info",
                "payload": {},
            },
            {
                "event_id": str(uuid.uuid4()),
                "type": "kitsune.run.succeeded",
                "occurred_at": utcnow().isoformat(),
                "agent_id": "demo-agent",
                "runtime_instance_id": instance_id,
                "run_id": run.run_id,
                "correlation_id": run.correlation_id,
                "severity": "info",
                "payload": {"output": {"fast": True}},
            },
        ]
        control.events_service.ingest("demo-agent", events)

    monkeypatch.setattr(control.runtime, "dispatch_resident", terminal_before_acceptance)
    try:
        assert await control.runs.dispatch_available() == 1
        with control.database.session() as session:
            stored = session.get(Run, run.run_id)
            assert stored is not None
            assert stored.status == "succeeded"
            assert stored.output == {"fast": True}
            assert stored.runtime_instance_id == instance_id
    finally:
        await _close(control)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError])
async def test_resident_transport_failure_keeps_assignment_for_idempotent_retry(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.HTTPError],
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="external", mode="resident")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            Handler(
                agent_id="demo-agent",
                name="default",
                default_timeout_seconds=30,
            )
        )
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                control_url="https://agent.example.invalid",
                started_at=utcnow(),
                ready_at=utcnow(),
            )
        )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
    )

    class FailingClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> FailingClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, url: str, **_: Any) -> Any:
            class FailingStream:
                async def __aenter__(self) -> httpx.Response:
                    request = httpx.Request(method, url)
                    raise error_type("deterministic transport failure", request=request)

                async def __aexit__(self, *_: Any) -> None:
                    return None

            return FailingStream()

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", FailingClient)
    try:
        assert await control.runs.dispatch_available() == 0
        with control.database.session() as session:
            uncertain = session.get(Run, run.run_id)
            assert uncertain is not None
            assert uncertain.status == "dispatching"
            assert uncertain.runtime_instance_id == instance_id
            assert uncertain.error is None

        async def accepted_retry(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(control.runtime, "dispatch_resident", accepted_retry)
        assert await control.runs.dispatch_available() == 1
        with control.database.session() as session:
            accepted = session.get(Run, run.run_id)
            assert accepted is not None
            assert accepted.status == "running"
            assert accepted.runtime_instance_id == instance_id
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_resident_dispatching_run_is_retried_after_workspace_restart(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="external", mode="resident")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(Handler(agent_id="demo-agent", name="default", default_timeout_seconds=30))
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                control_url="https://agent.example.invalid",
                started_at=utcnow(),
                ready_at=utcnow(),
            )
        )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
    )
    with control.database.session() as session:
        persisted = session.get(Run, run.run_id)
        assert persisted is not None
        persisted.status = "dispatching"
        persisted.dispatching_at = utcnow()
        persisted.runtime_instance_id = instance_id

    submitted: list[str] = []

    async def accepted(detached: Run, *_: Any) -> None:
        submitted.append(detached.run_id)

    monkeypatch.setattr(control.runtime, "dispatch_resident", accepted)
    try:
        assert await control.runs.dispatch_available() == 1
        assert submitted == [run.run_id]
        with control.database.session() as session:
            recovered = session.get(Run, run.run_id)
            assert recovered is not None and recovered.status == "running"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_resident_explicit_rejection_is_terminal(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="external", mode="resident")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(Handler(agent_id="demo-agent", name="default", default_timeout_seconds=30))
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                control_url="https://agent.example.invalid",
                started_at=utcnow(),
                ready_at=utcnow(),
            )
        )
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
    )

    async def rejected(*_: Any, **__: Any) -> None:
        raise runtime_module.RuntimeOperationError("Agent returned 409")

    monkeypatch.setattr(control.runtime, "dispatch_resident", rejected)
    try:
        assert await control.runs.dispatch_available() == 0
        with control.database.session() as session:
            failed = session.get(Run, run.run_id)
            assert failed is not None and failed.status == "failed"
            assert failed.error is not None
            assert failed.error["type"] == "dispatch_failed"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_restart_recovers_persisted_stopping_instance_before_container_cleanup(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _control(
        tmp_path,
        manifest_factory,
        adapter="docker",
        mode="ephemeral",
    )
    run, _ = first.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={},
        start_running=True,
    )
    instance_id = str(uuid.uuid4())
    recovery_path, _ = first.runtime._outbox_paths("demo-agent", instance_id, "docker")
    with first.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="docker",
                mode="ephemeral",
                status="stopping",
                container_id="container-stopping",
                started_at=utcnow(),
                runtime_metadata={
                    "outbox_path": str(recovery_path),
                    "ephemeral_run_id": run.run_id,
                },
            )
        )
        stored_run = session.get(Run, run.run_id)
        assert stored_run is not None
        stored_run.runtime_instance_id = instance_id

    container_path = tmp_path / "stopped-container" / "events.sqlite3"
    outbox = EventOutbox(container_path)
    assert await outbox.enqueue(
        KitsuneEvent.model_validate(
            {
                "event_id": str(uuid.uuid4()),
                "type": "kitsune.run.succeeded",
                "occurred_at": utcnow().isoformat(),
                "agent_id": "demo-agent",
                "runtime_instance_id": instance_id,
                "run_id": run.run_id,
                "correlation_id": run.correlation_id,
                "severity": "info",
                "payload": {"output": {"after_restart": True}},
            }
        )
    )
    first.telemetry.shutdown()
    first.database.dispose()

    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    second = ControlPlane(database, settings)
    removed: list[str] = []

    async def stop(*_: Any, **__: Any) -> None:
        return None

    async def export(_: str, destination: Path) -> bool:
        def copy() -> bool:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{container_path}{suffix}")
                if source.exists():
                    Path(f"{destination}{suffix}").write_bytes(source.read_bytes())
            return destination.exists()

        return await asyncio.to_thread(copy)

    async def remove(container_id: str) -> bool:
        removed.append(container_id)
        return True

    monkeypatch.setattr(second.runtime.docker, "stop", stop)
    monkeypatch.setattr(second.runtime.docker, "export_outbox", export)
    monkeypatch.setattr(second.runtime.docker, "archive_and_remove", remove)
    try:
        await second.runtime.monitor()
        with second.database.session() as session:
            stored_run = session.get(Run, run.run_id)
            stored_instance = session.get(RuntimeInstance, instance_id)
            assert stored_run is not None
            assert stored_run.status == "succeeded"
            assert stored_run.output == {"after_restart": True}
            assert stored_instance is not None
            assert stored_instance.status == "stopped"
            assert stored_instance.container_removed_at is not None
        assert removed == ["container-stopping"]
        assert not recovery_path.exists()
    finally:
        await _close(second)


@pytest.mark.asyncio
async def test_resident_exit_fails_every_nonterminal_assigned_run(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> None:
    control = _control(
        tmp_path,
        manifest_factory,
        adapter="process",
        mode="resident",
        max_concurrency=3,
    )
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="process",
                mode="resident",
                status="ready",
                started_at=utcnow(),
                ready_at=utcnow(),
                runtime_metadata={},
            )
        )
    runs = [
        control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={"index": index},
            start_running=True,
        )[0]
        for index in range(3)
    ]
    with control.database.session() as session:
        terminal = session.get(Run, runs[2].run_id)
        assert terminal is not None
        terminal.status = "succeeded"
        terminal.ended_at = utcnow()
        instance = session.get(RuntimeInstance, instance_id)
        assert instance is not None
        detached = instance
    try:
        await control.runtime._handle_exit(detached, 9)
        with control.database.session() as session:
            first = session.get(Run, runs[0].run_id)
            second = session.get(Run, runs[1].run_id)
            terminal = session.get(Run, runs[2].run_id)
            assert first is not None and first.status == "failed"
            assert second is not None and second.status == "failed"
            assert first.error is not None and first.error["type"] == "runtime_exit"
            assert second.error is not None and second.error["details"]["exit_code"] == 9
            assert terminal is not None and terminal.status == "succeeded"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_force_cancel_preserves_target_and_fails_other_resident_runs(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(
        tmp_path,
        manifest_factory,
        adapter="process",
        mode="resident",
        max_concurrency=2,
    )
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="process",
                mode="resident",
                status="ready",
                control_url="http://127.0.0.1:8081",
                started_at=utcnow(),
                ready_at=utcnow(),
                runtime_metadata={},
            )
        )
    target, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={"target": True},
        start_running=True,
    )
    other, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={"target": False},
        start_running=True,
    )

    async def cancel(*_: Any, **__: Any) -> None:
        return None

    async def not_terminal(*_: Any, **__: Any) -> bool:
        return False

    async def stop(*_: Any, **__: Any) -> int:
        return -9

    monkeypatch.setattr(control.runtime.external, "cancel", cancel)
    monkeypatch.setattr(control.runtime, "_wait_for_terminal", not_terminal)
    monkeypatch.setattr(control.runtime.process, "stop", stop)
    try:
        await control.runtime.cancel_run(target, force=True)
        with control.database.session() as session:
            stored_target = session.get(Run, target.run_id)
            stored_other = session.get(Run, other.run_id)
            assert stored_target is not None and stored_target.status == "cancelled"
            assert stored_other is not None and stored_other.status == "failed"
            assert stored_other.error is not None
            assert stored_other.error["type"] == "runtime_exit"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_terminal_event_winning_cancel_gap_does_not_kill_resident(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(
        tmp_path,
        manifest_factory,
        adapter="process",
        mode="resident",
        max_concurrency=2,
    )
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="process",
                mode="resident",
                status="ready",
                control_url="http://127.0.0.1:8081",
                started_at=utcnow(),
                ready_at=utcnow(),
                runtime_metadata={},
            )
        )
    target, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={"target": True},
        start_running=True,
    )
    other, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="on_demand",
        input_value={"target": False},
        start_running=True,
    )
    stopped = False

    async def cancel(*_: Any, **__: Any) -> None:
        return None

    async def terminal_arrives(*_: Any, **__: Any) -> bool:
        with control.database.session() as session:
            stored = session.get(Run, target.run_id)
            assert stored is not None
            stored.status = "succeeded"
            stored.ended_at = utcnow()
            stored.output = {"won": "event"}
        return False

    async def stop(*_: Any, **__: Any) -> int:
        nonlocal stopped
        stopped = True
        return -9

    monkeypatch.setattr(control.runtime.external, "cancel", cancel)
    monkeypatch.setattr(control.runtime, "_wait_for_terminal", terminal_arrives)
    monkeypatch.setattr(control.runtime.process, "stop", stop)
    try:
        await control.runtime.cancel_run(target, force=True)
        assert not stopped
        with control.database.session() as session:
            stored_target = session.get(Run, target.run_id)
            stored_other = session.get(Run, other.run_id)
            runtime = session.get(RuntimeInstance, instance_id)
            assert stored_target is not None and stored_target.status == "succeeded"
            assert stored_target.output == {"won": "event"}
            assert stored_other is not None and stored_other.status == "running"
            assert runtime is not None and runtime.status == "ready"
    finally:
        await _close(control)
