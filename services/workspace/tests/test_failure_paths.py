"""Failure-path tests for lifecycle, cancellation, health, and migrations."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import signal
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from conftest import ManifestFactory, settings_for
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from kitsune_workspace.app import create_app
from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import AgentDefinition, Run, RuntimeInstance
from kitsune_workspace.runtime import DockerAdapter, ProcessAdapter, RuntimeOperationError
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
async def test_restart_backoff_is_nonblocking_and_shutdown_cancels_it(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="process")
    with control.database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["runtime"]["restart"] = {
            "policy": "on_failure",
            "max_attempts": 2,
            "backoff_seconds": 1,
            "max_backoff_seconds": 1,
            "reset_after_seconds": 300,
        }
        definition.snapshot = snapshot

    async def start_without_child(*_: Any, **__: Any) -> dict[str, Any]:
        return {"pid": None}

    monkeypatch.setattr(control.runtime.process, "start", start_without_child)
    monkeypatch.setattr(control.runtime.process, "status", lambda _: (False, 1))
    try:
        await control.runtime.start_agent("demo-agent")
        started = time.monotonic()
        await control.runtime.monitor()
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert len(control.runtime._restart_tasks) == 1
    finally:
        await _close(control)
    assert not control.runtime._restart_tasks


@pytest.mark.asyncio
async def test_crash_loop_stops_at_max_attempts(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="process")
    with control.database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["runtime"]["restart"] = {
            "policy": "on_failure",
            "max_attempts": 2,
            "backoff_seconds": 0.01,
            "max_backoff_seconds": 0.02,
            "reset_after_seconds": 300,
        }
        definition.snapshot = snapshot

    async def start_without_child(*_: Any, **__: Any) -> dict[str, Any]:
        return {"pid": None}

    monkeypatch.setattr(control.runtime.process, "start", start_without_child)
    monkeypatch.setattr(control.runtime.process, "status", lambda _: (False, 1))
    try:
        await control.runtime.start_agent("demo-agent")
        deadline = time.monotonic() + 3
        instances: list[RuntimeInstance] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            await control.runtime.monitor()
            with control.database.session() as session:
                instances = list(
                    session.query(RuntimeInstance)
                    .filter(RuntimeInstance.agent_id == "demo-agent")
                    .order_by(RuntimeInstance.started_at)
                )
            if len(instances) == 3 and instances[-1].last_error:
                break
        assert [item.restart_attempts for item in instances] == [0, 1, 2]
        assert all(item.status == "failed" for item in instances)
        assert instances[-1].last_error == "restart policy reached max_attempts"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_restart_attempts_reset_after_stable_uptime(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="process")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["runtime"]["restart"] = {
            "policy": "always",
            "max_attempts": 3,
            "backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "reset_after_seconds": 10,
        }
        definition.snapshot = snapshot
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="process",
                mode="resident",
                status="ready",
                restart_attempts=3,
                started_at=utcnow() - timedelta(seconds=20),
                runtime_metadata={},
            )
        )
    attempts: list[int] = []

    async def record_start(
        agent_id: str, run: Run | None = None, restart_attempt: int = 0
    ) -> RuntimeInstance:
        assert agent_id == "demo-agent" and run is None
        attempts.append(restart_attempt)
        with control.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            assert instance is not None
            return instance

    monkeypatch.setattr(control.runtime, "start_agent", record_start)
    try:
        with control.database.session() as session:
            detached = session.get(RuntimeInstance, instance_id)
            assert detached is not None
        await control.runtime._handle_exit(detached, 0)
        await asyncio.gather(*control.runtime._restart_tasks)
        assert attempts == [1]
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_heartbeat_loss_marks_external_runtime_lost(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="external",
                mode="resident",
                status="ready",
                started_at=utcnow() - timedelta(minutes=5),
                ready_at=utcnow() - timedelta(minutes=5),
                last_heartbeat_at=utcnow() - timedelta(minutes=5),
                runtime_metadata={},
            )
        )
    try:
        control.runtime.workspace_started_at = utcnow()
        await control.runtime.monitor()
        with control.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            assert instance is not None and instance.status == "ready"

        control.runtime.workspace_started_at = utcnow() - timedelta(minutes=5)
        await control.runtime.monitor()
        with control.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            assert instance is not None and instance.status == "lost"
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_succeeded_run_stays_succeeded_when_ephemeral_runtime_exits(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="process", mode="ephemeral")
    run, _ = control.runs.create(
        agent_id="demo-agent",
        handler="default",
        source="self",
        input_value={},
        start_running=True,
    )
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="process",
                mode="ephemeral",
                status="ready",
                started_at=utcnow(),
                runtime_metadata={"ephemeral_run_id": run.run_id},
            )
        )
        session.flush()
        stored = session.get(Run, run.run_id)
        assert stored is not None
        stored.runtime_instance_id = instance_id
    control.events_service.ingest(
        "demo-agent",
        [
            {
                "event_id": str(uuid.uuid4()),
                "type": "kitsune.run.succeeded",
                "occurred_at": utcnow().isoformat(),
                "agent_id": "demo-agent",
                "runtime_instance_id": instance_id,
                "run_id": run.run_id,
                "correlation_id": run.correlation_id,
                "severity": "info",
                "payload": {"output": {"answer": 42}},
            }
        ],
    )
    published: list[tuple[str, dict[str, Any]]] = []

    async def capture(event_type: str, body: dict[str, Any]) -> None:
        published.append((event_type, body))

    monkeypatch.setattr(control.runtime, "publish", capture)
    try:
        with control.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            assert instance is not None
        await control.runtime._handle_exit(instance, 0)
        with control.database.session() as session:
            stored = session.get(Run, run.run_id)
            assert stored is not None
            assert stored.status == "succeeded"
            assert stored.output == {"answer": 42}
        assert not any(
            event_type == "run" and body.get("status") == "failed" for event_type, body in published
        )
    finally:
        await _close(control)


@pytest.mark.asyncio
async def test_process_ignoring_cancel_is_killed_after_grace_period() -> None:
    adapter = ProcessAdapter()
    snapshot = {
        "spec": {
            "runtime": {
                "process": {
                    "command": [
                        "/bin/sh",
                        "-c",
                        "trap '' TERM; while :; do sleep 1; done",
                    ]
                },
                "environment": {},
                "secrets": {},
            }
        }
    }
    await adapter.start("ignores-cancel", snapshot)
    await asyncio.sleep(0.1)
    started = time.monotonic()
    exit_code = await adapter.stop("ignores-cancel", grace_seconds=1)
    elapsed = time.monotonic() - started
    assert exit_code == -signal.SIGKILL
    assert 0.8 <= elapsed < 3
    await adapter.close()


@pytest.mark.asyncio
async def test_expired_run_stops_ephemeral_process_and_becomes_timed_out(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="process", mode="ephemeral")
    with control.database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["runtime"]["process"]["command"] = [
            "/bin/sh",
            "-c",
            "trap '' TERM; while :; do sleep 1; done",
        ]
        definition.snapshot = snapshot
    try:
        run, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={},
            timeout_seconds=30,
        )
        assert await control.runs.dispatch_available() == 1
        await asyncio.sleep(0.1)
        with control.database.session() as session:
            stored = session.get(Run, run.run_id)
            assert stored is not None
            stored.deadline = utcnow() - timedelta(seconds=1)
        assert await control.runs.enforce_timeouts() == 1
        with control.database.session() as session:
            stored = session.get(Run, run.run_id)
            instance = session.get(RuntimeInstance, stored.runtime_instance_id if stored else "")
            assert stored is not None and stored.status == "timed_out"
            assert instance is not None and instance.status == "stopped"
            assert instance.last_exit_code == -signal.SIGKILL
    finally:
        await _close(control)


def test_docker_daemon_and_container_start_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable = DockerAdapter(tmp_path / "missing.sock")
    with pytest.raises(RuntimeOperationError, match="unavailable"):
        unavailable._client()

    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket)
    requests: list[tuple[str, str]] = []

    async def request(method: str, path: str, **_: Any) -> httpx.Response:
        requests.append((method, path))
        if path.startswith("/networks/"):
            return httpx.Response(
                200,
                json={
                    "Id": "network-id",
                    "Name": "test-agent-network",
                    "Driver": "bridge",
                    "Scope": "local",
                },
            )
        if path.startswith("/containers/create"):
            return httpx.Response(201, json={"Id": "container-failed"})
        if path.endswith("/start"):
            raise RuntimeOperationError("Docker daemon stopped")
        return httpx.Response(204)

    monkeypatch.setattr(adapter, "_request", request)
    snapshot = {
        "spec": {
            "runtime": {
                "docker": {
                    "image": "example.invalid/agent:latest",
                    "network": "test-agent-network",
                    "volumes": [],
                },
                "environment": {},
                "secrets": {},
            }
        }
    }
    with pytest.raises(RuntimeOperationError, match="stopped"):
        asyncio.run(adapter.start("00000000-0000-0000-0000-000000000001", "demo-agent", snapshot))
    assert requests[-1] == ("DELETE", "/containers/container-failed?force=true")


@pytest.mark.asyncio
async def test_terminal_docker_containers_are_removed_with_bounded_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket)
    adapter.archived_log_limit = 2
    requests: list[tuple[str, str]] = []

    async def request(method: str, path: str, **_: Any) -> httpx.Response:
        requests.append((method, path))
        return httpx.Response(204)

    async def stream_request(method: str, path: str, **_: Any) -> bytes:
        requests.append((method, path))
        container_id = path.split("/")[2]
        return f"tail-{container_id}\n".encode()

    monkeypatch.setattr(adapter, "_request", request)
    monkeypatch.setattr(adapter, "_stream_request", stream_request)
    for container_id in ("container-1", "container-2", "container-3"):
        assert await adapter.archive_and_remove(container_id)
    delete_paths = [path for method, path in requests if method == "DELETE"]
    assert delete_paths == [
        "/containers/container-1?force=true",
        "/containers/container-2?force=true",
        "/containers/container-3?force=true",
    ]
    assert list(adapter.archived_logs) == ["container-2", "container-3"]
    assert await adapter.tail("container-2", 1) == ["tail-container-2"]


@pytest.mark.asyncio
async def test_docker_stop_failure_keeps_runtime_for_recovery_retry(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path, manifest_factory, adapter="docker")
    instance_id = str(uuid.uuid4())
    with control.database.session() as session:
        session.add(
            RuntimeInstance(
                runtime_instance_id=instance_id,
                agent_id="demo-agent",
                adapter="docker",
                mode="resident",
                status="ready",
                container_id="container-one",
                started_at=utcnow(),
                runtime_metadata={},
            )
        )

    async def fail_stop(*_: Any, **__: Any) -> None:
        raise RuntimeOperationError("Docker daemon unavailable")

    monkeypatch.setattr(control.runtime.docker, "stop", fail_stop)
    try:
        result = await control.runtime.stop_instance(instance_id)
        assert result.status == "stopping"
        assert result.stopped_at is None
        assert result.last_error == "Runtime stop failed (RuntimeOperationError)"
        assert result.container_id == "container-one"
    finally:
        await _close(control)


def test_database_disconnect_and_recovery_are_reported(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory()
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        database = client.app.state.database
        original_session = database.session

        @contextlib.contextmanager
        def disconnected() -> Iterator[None]:
            raise OSError("database disconnected")
            yield

        monkeypatch.setattr(database, "session", disconnected)
        assert client.get("/healthz").status_code == 503
        degraded = client.get("/api/health")
        assert degraded.status_code == 200
        assert degraded.json()["database"] == "unhealthy"
        assert degraded.json()["status"] == "degraded"
        monkeypatch.setattr(database, "session", original_session)
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/health").json()["database"] == "healthy"


def test_alembic_upgrade_and_downgrade_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "migrated.sqlite3"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("KITSUNE_WORKSPACE__DATABASE_URL", database_url)
    service_root = Path(__file__).resolve().parents[1]
    configuration = Config(service_root / "alembic.ini")
    command.upgrade(configuration, "head")
    database = Database(database_url)
    database.require_migrated_schema()
    assert "agent_definitions" in inspect(database.engine).get_table_names()
    assert "alembic_version" in inspect(database.engine).get_table_names()
    database.dispose()
    command.downgrade(configuration, "base")
    database = Database(database_url)
    assert "agent_definitions" not in inspect(database.engine).get_table_names()
    with pytest.raises(RuntimeError, match="schema revision is none"):
        database.require_migrated_schema()
    database.dispose()
