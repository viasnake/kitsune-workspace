"""Manifest atomicity, queue admission, desired state, scheduling, and retention tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import ManifestFactory, settings_for
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

import kitsune_workspace.cli as workspace_cli
from kitsune_workspace.app import create_app
from kitsune_workspace.cli import workspace_app
from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database, InstanceLock
from kitsune_workspace.manifest import ManifestRegistry, ManifestReloadError
from kitsune_workspace.models import (
    AgentDefinition,
    AuditRecord,
    Event,
    Run,
    Schedule,
    UsageRecord,
    WorkspaceLock,
)
from kitsune_workspace.services import QueueCapacityExceeded, RetentionService, RunServiceError
from kitsune_workspace.util import ensure_aware, utcnow


def _control(root: Path, manifest_factory: ManifestFactory) -> ControlPlane:
    manifest_factory()
    settings = settings_for(root)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    return control


def _close(control: ControlPlane) -> None:
    control.telemetry.shutdown()
    control.database.dispose()


def test_manifest_reload_is_atomic_and_preserves_secret_references(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    path = manifest_factory()
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    registry = ManifestRegistry(database, settings)
    first = registry.reload()
    with database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        original_hash = definition.content_hash
        assert definition.snapshot["spec"]["security"]["agent_token_ref"].startswith("env://")
    path.write_text("schema: kitsune.agent\nrevision: broken\n", encoding="utf-8")
    with pytest.raises(ManifestReloadError):
        registry.reload()
    with database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        assert definition.content_hash == original_hash
        assert definition.active
    database.dispose()
    assert first["added"] == ["demo-agent"]


def test_manifest_reload_surfaces_never_echo_invalid_literal_values(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registry, API, operator CLI, and logs expose only structural validation metadata."""

    sentinel = "workspace-manifest-literal-secret"
    path = manifest_factory()
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        path.write_text(
            path.read_text(encoding="utf-8").replace("revision: 1", f"revision: {sentinel}"),
            encoding="utf-8",
        )
        response = client.post("/api/admin/reload")
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace(f"revision: {sentinel}", "revision: 1")
            .rstrip()
            + f"\n{sentinel}: forbidden\n",
            encoding="utf-8",
        )
        unknown_field = client.post("/api/admin/reload")

    assert response.status_code == 422
    assert sentinel not in response.text
    issue = response.json()["errors"][0]["error"][0]
    assert set(issue) == {"loc", "type", "msg"}
    assert issue["loc"] == ["revision"]
    assert issue["msg"] == "Manifest value is invalid"
    assert sentinel not in caplog.text

    assert unknown_field.status_code == 422
    assert sentinel not in unknown_field.text
    assert unknown_field.json()["errors"][0]["error"][0]["loc"][-1] == "<extra-field>"

    monkeypatch.setattr(workspace_cli.httpx, "request", lambda *_args, **_kwargs: response)
    cli_result = CliRunner().invoke(
        workspace_app,
        ["reload", "--url", "http://127.0.0.1:8080"],
    )
    assert cli_result.exit_code == 1
    assert sentinel not in cli_result.output


@pytest.mark.parametrize(
    ("adapter", "old", "new"),
    [
        (
            "process",
            "control_url: http://127.0.0.1:8081",
            "control_url: https://127.0.0.1:8081",
        ),
        (
            "process",
            "control_url: http://127.0.0.1:8081",
            "control_url: http://agent.internal:8081",
        ),
        (
            "docker",
            "control_port: 8081",
            "control_port: 80",
        ),
        (
            "docker",
            "volumes: []",
            'volumes: ["/tmp/outbox:/var/lib/kitsune-outbox:rw"]',
        ),
    ],
)
def test_manifest_rejects_unservable_managed_control_and_reserved_outbox(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    adapter: str,
    old: str,
    new: str,
) -> None:
    path = manifest_factory(adapter=adapter, mode="resident")
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    try:
        with pytest.raises(ManifestReloadError):
            ManifestRegistry(database, settings).reload()
    finally:
        database.dispose()


def test_burst_admission_enforces_concurrency_plus_queue_capacity(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    try:
        for index in range(3):
            run, created = control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={"index": index},
            )
            assert created and run.status == "queued"
        with pytest.raises(QueueCapacityExceeded):
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={"index": 4},
            )
    finally:
        _close(control)


def test_parallel_sqlite_admission_cannot_oversubscribe_capacity(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(max_concurrency=1, queue_capacity=1)
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    barrier = threading.Barrier(8)

    def create(index: int) -> str:
        barrier.wait()
        try:
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={"index": index},
            )
        except QueueCapacityExceeded:
            return "rejected"
        return "accepted"

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(create, range(8)))
        assert outcomes.count("accepted") == 2
        assert outcomes.count("rejected") == 6
        with database.session() as session:
            assert session.query(Run).count() == 2
    finally:
        _close(control)


def test_reject_policy_rejects_second_undispatched_request(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(queue_policy="reject", queue_capacity=10)
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    try:
        control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={},
        )
        with pytest.raises(QueueCapacityExceeded):
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={},
            )
    finally:
        _close(control)


def test_stopped_agent_rejects_new_run_for_managed_and_external(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(desired_state="stopped")
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    try:
        with pytest.raises(RunServiceError, match="desired_state"):
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={},
            )
    finally:
        _close(control)


@pytest.mark.asyncio
async def test_schedule_skip_and_replace_overlap(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    trigger = """    - id: periodic
      type: schedule
      handler: default
      cron: \"* * * * *\"
      timezone: UTC
      overlap: skip
      misfire_grace_seconds: 300
"""
    manifest_factory(queue_capacity=5, triggers=trigger)
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    try:
        active, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="schedule",
            trigger_id="periodic",
            input_value={},
            start_running=True,
        )
        with database.session() as session:
            schedule = session.scalar(select(Schedule))
            assert schedule is not None
            schedule.next_fire_at = utcnow()
        assert await control.dispatch_schedules() == 0
        with database.session() as session:
            schedule = session.scalar(select(Schedule))
            assert schedule is not None and schedule.last_outcome == "overlap_skipped"
            schedule.overlap = "replace"
            schedule.next_fire_at = utcnow()
        assert await control.dispatch_schedules() == 1
        with database.session() as session:
            assert session.get(Run, active.run_id).status == "cancelled"
    finally:
        _close(control)


@pytest.mark.asyncio
async def test_stale_schedule_cursor_advances_directly_to_future_occurrence(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    trigger = """    - id: periodic
      type: schedule
      handler: default
      cron: "* * * * *"
      timezone: UTC
      overlap: skip
      misfire_grace_seconds: 300
"""
    manifest_factory(triggers=trigger)
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    before = utcnow()
    try:
        with database.session() as session:
            schedule = session.scalar(select(Schedule))
            assert schedule is not None
            schedule.next_fire_at = before - timedelta(days=30)

        assert await control.dispatch_schedules() == 0
        with database.session() as session:
            schedule = session.scalar(select(Schedule))
            assert schedule is not None
            assert ensure_aware(schedule.next_fire_at) > before
            assert schedule.last_outcome == "misfire_skipped"
            assert session.query(Run).count() == 0
    finally:
        _close(control)


def test_instance_lock_excludes_a_second_control_plane(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'lock.sqlite3'}")
    database.create_schema()
    first = InstanceLock(database, "workspace:test", 30)
    second = InstanceLock(database, "workspace:test", 30)
    first.acquire()
    with pytest.raises(RuntimeError, match="held"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()
    database.dispose()


def test_instance_lock_serializes_concurrent_expired_takeover(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'lock-takeover.sqlite3'}")
    database.create_schema()
    with database.session() as session:
        session.add(
            WorkspaceLock(
                lock_name="workspace:takeover",
                owner_id="expired-owner",
                acquired_at=utcnow() - timedelta(minutes=2),
                expires_at=utcnow() - timedelta(minutes=1),
            )
        )
    contenders = [InstanceLock(database, "workspace:takeover", 30) for _ in range(2)]
    barrier = threading.Barrier(2)

    def acquire(lock: InstanceLock) -> bool:
        barrier.wait()
        try:
            lock.acquire()
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, contenders))
    assert outcomes.count(True) == 1
    with database.session() as session:
        stored = session.get(WorkspaceLock, "workspace:takeover")
        assert stored is not None
        assert stored.owner_id == contenders[outcomes.index(True)].owner_id
    database.dispose()


def test_stale_instance_lock_cannot_renew_or_release_a_new_owner(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'lock-stale-owner.sqlite3'}")
    database.create_schema()
    stale = InstanceLock(database, "workspace:stale-owner", 30)
    current = InstanceLock(database, "workspace:stale-owner", 30)
    stale.acquire()
    with database.session(fence=False) as session:
        stored = session.get(WorkspaceLock, "workspace:stale-owner")
        assert stored is not None
        stored.expires_at = utcnow() - timedelta(seconds=1)
    current.acquire()

    with pytest.raises(RuntimeError, match="ownership was lost"):
        stale.renew()
    stale.acquired = True
    stale.release()

    with database.session() as session:
        stored = session.get(WorkspaceLock, "workspace:stale-owner")
        assert stored is not None
        assert stored.owner_id == current.owner_id
        assert ensure_aware(stored.expires_at) > utcnow()
    current.release()
    database.dispose()


def test_database_fence_rolls_back_old_owner_after_takeover(tmp_path: Path) -> None:
    path = tmp_path / "lock-fence.sqlite3"
    old_database = Database(f"sqlite:///{path}")
    old_database.create_schema()
    old = InstanceLock(old_database, "workspace:fence", 30)
    old.acquire()
    assert old.locally_valid
    with old_database.session(fence=False) as session:
        stored = session.get(WorkspaceLock, old.name)
        assert stored is not None
        stored.expires_at = utcnow() - timedelta(seconds=1)

    new_database = Database(f"sqlite:///{path}")
    current = InstanceLock(new_database, "workspace:fence", 30)
    current.acquire()
    with new_database.session() as session:
        assert current.fence(session).owner_id == current.owner_id

    with pytest.raises(RuntimeError, match="ownership was lost"):
        with old_database.session() as session:
            session.add(
                AuditRecord(
                    occurred_at=utcnow(),
                    actor_type="system",
                    actor_id="stale-owner",
                    action="stale.write",
                    resource_type="workspace",
                    outcome="succeeded",
                    details={},
                )
            )
    assert not old.locally_valid
    assert old_database.active_instance_lock is old
    with pytest.raises(RuntimeError, match="ownership was lost"):
        with old_database.session() as session:
            session.add(
                AuditRecord(
                    occurred_at=utcnow(),
                    actor_type="system",
                    actor_id="stale-owner",
                    action="later.stale.write",
                    resource_type="workspace",
                    outcome="succeeded",
                    details={},
                )
            )
    with new_database.session() as session:
        assert session.query(AuditRecord).count() == 0

    old.release()
    assert old_database.active_instance_lock is None
    current.release()
    old_database.dispose()
    new_database.dispose()


def test_database_fence_rejects_transaction_admitted_before_takeover(tmp_path: Path) -> None:
    path = tmp_path / "lock-admitted-before-takeover.sqlite3"
    old_database = Database(f"sqlite:///{path}")
    old_database.create_schema()
    old = InstanceLock(old_database, "workspace:admitted", 30)
    old.acquire()
    session_opened = threading.Event()
    takeover_complete = threading.Event()

    def stale_operation() -> None:
        with old_database.session() as session:
            session_opened.set()
            assert takeover_complete.wait(timeout=10)
            session.add(
                AuditRecord(
                    occurred_at=utcnow(),
                    actor_type="system",
                    actor_id="stale-owner",
                    action="admitted.stale.write",
                    resource_type="workspace",
                    outcome="succeeded",
                    details={},
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(stale_operation)
        assert session_opened.wait(timeout=10)
        with old_database.session(fence=False) as session:
            stored = session.get(WorkspaceLock, old.name)
            assert stored is not None
            stored.expires_at = utcnow() - timedelta(seconds=1)
        current_database = Database(f"sqlite:///{path}")
        current = InstanceLock(current_database, "workspace:admitted", 30)
        current.acquire()
        takeover_complete.set()
        with pytest.raises(RuntimeError, match="ownership was lost"):
            pending.result(timeout=10)

    assert old_database.active_instance_lock is old
    with pytest.raises(RuntimeError, match="ownership was lost"):
        with old_database.session() as session:
            session.add(
                AuditRecord(
                    occurred_at=utcnow(),
                    actor_type="system",
                    actor_id="stale-owner",
                    action="later.stale.write",
                    resource_type="workspace",
                    outcome="succeeded",
                    details={},
                )
            )
    with current_database.session() as session:
        assert session.query(AuditRecord).count() == 0

    old.release()
    current.release()
    old_database.dispose()
    current_database.dispose()


def test_retention_removes_expired_records(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    old = utcnow() - timedelta(days=400)
    try:
        run, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="self",
            input_value={},
            start_running=True,
        )
        with control.database.session() as session:
            _, storage_usage = control.runtime.storage.lock(session, "demo-agent")
            assert storage_usage is not None
            stored = session.get(Run, run.run_id)
            assert stored is not None
            stored.status = "failed"
            stored.ended_at = old
            stored.retention_days = 1
            event = Event(
                event_id="00000000-0000-4000-8000-000000000099",
                type="kitsune.test.old",
                occurred_at=old,
                received_at=old,
                agent_id="demo-agent",
                severity="info",
                payload={},
                storage_charge_bytes=2,
            )
            usage = UsageRecord(
                agent_id="demo-agent", recorded_at=old, raw={}, storage_charge_bytes=2
            )
            control.runtime.storage.reserve(storage_usage, events=1, payload_bytes=4)
            session.add_all([event, usage])
            session.add(
                AuditRecord(
                    occurred_at=old,
                    actor_type="system",
                    actor_id="test",
                    action="test.old",
                    resource_type="workspace",
                    outcome="succeeded",
                    details={},
                )
            )
        result = RetentionService(control.database, control.settings).run()
        assert result["runs"] == 1
        assert result["events"] == 1
        assert result["usage_records"] == 1
        assert result["audit_records"] == 1
    finally:
        _close(control)


def test_run_retention_keeps_and_removes_linked_event_usage_as_one_unit(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    now = utcnow()
    try:
        long_run, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="self",
            input_value={},
            start_running=True,
        )
        with control.database.session() as session:
            completed = session.get(Run, long_run.run_id)
            assert completed is not None
            completed.status = "succeeded"
            completed.ended_at = now
        short_run, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="self",
            input_value={},
            start_running=True,
        )
        with control.database.session() as session:
            _, storage_usage = control.runtime.storage.lock(session, "demo-agent")
            assert storage_usage is not None
            long_stored = session.get(Run, long_run.run_id)
            short_stored = session.get(Run, short_run.run_id)
            assert long_stored is not None and short_stored is not None
            long_stored.status = "succeeded"
            long_stored.ended_at = now - timedelta(days=40)
            long_stored.retention_days = 400
            short_stored.status = "succeeded"
            short_stored.ended_at = now - timedelta(days=2)
            short_stored.retention_days = 1
            for prefix, run_id, occurred_at in (
                ("long", long_run.run_id, now - timedelta(days=40)),
                ("short", short_run.run_id, now),
            ):
                event_id = (
                    "00000000-0000-4000-8000-000000000101"
                    if prefix == "long"
                    else "00000000-0000-4000-8000-000000000102"
                )
                session.add(
                    Event(
                        event_id=event_id,
                        type=f"kitsune.test.{prefix}",
                        occurred_at=occurred_at,
                        received_at=occurred_at,
                        agent_id="demo-agent",
                        run_id=run_id,
                        severity="info",
                        payload={},
                        storage_charge_bytes=2,
                    )
                )
                session.add(
                    UsageRecord(
                        agent_id="demo-agent",
                        run_id=run_id,
                        recorded_at=occurred_at,
                        raw={},
                        storage_charge_bytes=2,
                    )
                )
            control.runtime.storage.reserve(storage_usage, events=2, payload_bytes=8)
        result = RetentionService(control.database, control.settings).run(now)
        assert result["runs"] == 1
        assert result["events"] == 1
        assert result["usage_records"] == 1
        with control.database.session() as session:
            assert session.get(Run, long_run.run_id) is not None
            assert session.get(Run, short_run.run_id) is None
            assert session.query(Event).one().run_id == long_run.run_id
            assert session.query(UsageRecord).one().run_id == long_run.run_id
    finally:
        _close(control)
