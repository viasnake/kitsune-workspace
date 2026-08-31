"""Transactional retained-storage quota regressions."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import ManifestFactory, agent_headers, registration, settings_for
from fastapi.testclient import TestClient

from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import AgentStorageUsage, Event, Run, RuntimeInstance
from kitsune_workspace.storage import (
    RUN_TERMINAL_ERROR_HEADROOM_BYTES,
    RUN_TERMINAL_EVENT_HEADROOM_BYTES,
    StorageQuotaExceeded,
    compact_json_charge,
)


def _control(root: Path, manifest_factory: ManifestFactory) -> ControlPlane:
    manifest_factory(max_concurrency=10, queue_capacity=20)
    settings = settings_for(root)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    return control


def _close(control: ControlPlane) -> None:
    control.telemetry.shutdown()
    control.database.dispose()


def _registered_run(client: TestClient) -> tuple[str, dict[str, Any]]:
    runtime_id = str(uuid.uuid4())
    registered = client.post(
        "/api/agent/register",
        json=registration(runtime_id=runtime_id),
        headers=agent_headers(),
    )
    assert registered.status_code == 201, registered.text
    with client.app.state.database.session() as session:
        instance = session.get(RuntimeInstance, runtime_id)
        assert instance is not None
        instance.status = "ready"
        instance.ready_at = instance.ready_at or instance.started_at
    run_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    begun = client.post(
        "/api/agent/runs/begin",
        json={
            "run_id": run_id,
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "handler": "default",
            "source": "self",
            "correlation_id": correlation_id,
            "input": {},
        },
        headers=agent_headers(),
    )
    assert begun.status_code == 201, begun.text
    return runtime_id, begun.json()


def _run_event(
    run: dict[str, Any], runtime_id: str, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "occurred_at": "2026-08-24T01:00:00Z",
        "agent_id": "demo-agent",
        "runtime_instance_id": runtime_id,
        "run_id": run["run_id"],
        "correlation_id": run["correlation_id"],
        "severity": "info",
        "payload": payload,
    }


def test_manifest_creates_storage_usage_and_idempotent_run_replay_costs_zero(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    control.settings.events.max_runs_per_agent = 1
    try:
        created, was_created = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={"value": "kept"},
            idempotency_key="same-request",
        )
        replay, replay_created = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={"value": "kept"},
            idempotency_key="same-request",
        )
        assert replay.run_id == created.run_id
        assert was_created is True
        assert replay_created is False
        with control.database.session() as session:
            usage = session.get(AgentStorageUsage, "demo-agent")
            assert usage is not None
            assert usage.retained_run_count == 1
            assert usage.reserved_payload_bytes == (
                compact_json_charge({"value": "kept"})
                + RUN_TERMINAL_ERROR_HEADROOM_BYTES
                + RUN_TERMINAL_EVENT_HEADROOM_BYTES
            )
        with pytest.raises(StorageQuotaExceeded, match="Run count"):
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value={},
            )
    finally:
        _close(control)


def test_run_json_budget_accepts_exact_limit_and_rejects_one_more_byte(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    exact_input = {"value": "exact"}
    control.settings.events.max_reserved_json_bytes_per_agent = (
        compact_json_charge(exact_input)
        + RUN_TERMINAL_ERROR_HEADROOM_BYTES
        + RUN_TERMINAL_EVENT_HEADROOM_BYTES
    )
    try:
        control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value=exact_input,
        )
        with pytest.raises(StorageQuotaExceeded, match="reserved JSON bytes"):
            control.runs.create(
                agent_id="demo-agent",
                handler="default",
                source="on_demand",
                input_value=None,
            )
        with control.database.session() as session:
            assert session.query(Run).count() == 1
    finally:
        _close(control)


def test_missing_quota_row_fails_closed_as_an_integrity_error(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    control = _control(tmp_path, manifest_factory)
    try:
        with control.database.session() as session:
            session.delete(session.get(AgentStorageUsage, "demo-agent"))
        with pytest.raises(RuntimeError, match="storage usage row is missing"):
            with control.database.session() as session:
                control.runtime.storage.lock(session, "demo-agent")
    finally:
        _close(control)


def test_event_quota_is_atomic_and_duplicate_only_replay_costs_zero(
    client: TestClient,
) -> None:
    runtime_id, run = _registered_run(client)
    progress = _run_event(run, runtime_id, "kitsune.custom.progress", {})
    charge = compact_json_charge(progress["payload"])
    with client.app.state.database.session() as session:
        usage = session.get(AgentStorageUsage, "demo-agent")
        assert usage is not None
        client.app.state.settings.events.max_reserved_json_bytes_per_agent = (
            usage.reserved_payload_bytes + charge
        )
        client.app.state.settings.events.max_events_per_agent = 2

    first = client.post(
        "/api/agent/events/batch",
        json={"events": [progress]},
        headers=agent_headers(),
    )
    assert first.status_code == 202, first.text
    with client.app.state.database.session() as session:
        exact = session.get(AgentStorageUsage, "demo-agent")
        assert exact is not None
        exact_version = exact.lock_version
        exact_bytes = exact.reserved_payload_bytes

    replay = client.post(
        "/api/agent/events/batch",
        json={"events": [progress]},
        headers=agent_headers(),
    )
    assert replay.status_code == 202
    assert replay.json() == {"accepted": 0, "duplicates": [progress["event_id"]]}
    with client.app.state.database.session() as session:
        unchanged = session.get(AgentStorageUsage, "demo-agent")
        assert unchanged is not None
        assert unchanged.lock_version == exact_version
        assert unchanged.reserved_payload_bytes == exact_bytes

    rejected = _run_event(run, runtime_id, "kitsune.custom.progress", {})
    mixed = client.post(
        "/api/agent/events/batch",
        json={"events": [progress, rejected]},
        headers=agent_headers(),
    )
    assert mixed.status_code == 429
    assert mixed.headers["Retry-After"] == "60"
    with client.app.state.database.session() as session:
        assert session.get(Event, rejected["event_id"]) is None
        unchanged = session.get(AgentStorageUsage, "demo-agent")
        assert unchanged is not None
        assert unchanged.retained_event_count == 1
        assert unchanged.lock_version == exact_version


def test_terminal_event_uses_reserved_headroom_at_exact_agent_quota(
    client: TestClient,
) -> None:
    runtime_id, run = _registered_run(client)
    with client.app.state.database.session() as session:
        usage = session.get(AgentStorageUsage, "demo-agent")
        assert usage is not None
        exact_bytes = usage.reserved_payload_bytes
        client.app.state.settings.events.max_reserved_json_bytes_per_agent = exact_bytes
        client.app.state.settings.events.max_events_per_agent = 1
        client.app.state.settings.events.max_events_per_run = 1
    completion = _run_event(
        run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"value": "requires Run growth"}},
    )

    response = client.post(
        "/api/agent/events/batch",
        json={"events": [completion]},
        headers=agent_headers(),
    )
    assert response.status_code == 202, response.text
    with client.app.state.database.session() as session:
        stored_run = session.get(Run, run["run_id"])
        stored_event = session.get(Event, completion["event_id"])
        usage = session.get(AgentStorageUsage, "demo-agent")
        assert stored_run is not None and stored_event is not None and usage is not None
        assert stored_run.status == "succeeded"
        assert stored_run.output is None
        assert stored_event.payload == {"persistence_rejected": {"reason": "agent_storage_quota"}}
        assert usage.retained_event_count == 1
        assert usage.reserved_payload_bytes == exact_bytes
