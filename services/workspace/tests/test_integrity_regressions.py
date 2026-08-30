"""Regression coverage for Run lineage, assignment, and terminal integrity."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import ManifestFactory, agent_headers, registration, settings_for
from fastapi.testclient import TestClient

from kitsune_workspace.app import create_app
from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import (
    AgentDefinition,
    Event,
    Handler,
    Run,
    RuntimeInstance,
    UsageRecord,
)
from kitsune_workspace.services import RunServiceError


def _register(client: TestClient, runtime_id: str | None = None) -> str:
    response = client.post(
        "/api/agent/register",
        json=registration(runtime_id=runtime_id),
        headers=agent_headers(),
    )
    assert response.status_code == 201, response.text
    registered_id = response.json()["runtime_instance_id"]
    with client.app.state.database.session() as session:
        instance = session.get(RuntimeInstance, registered_id)
        assert instance is not None
        instance.status = "ready"
        instance.ready_at = instance.ready_at or instance.started_at
    return registered_id


def _begin(
    client: TestClient,
    *,
    runtime_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    runtime_id = runtime_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, "runtime.demo-agent"))
    body: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "agent_id": "demo-agent",
        "runtime_instance_id": runtime_id,
        "handler": "default",
        "source": "self",
        "correlation_id": str(uuid.uuid4()),
        "input": {"message": "hello"},
    }
    if trace_id is not None:
        body["trace_id"] = trace_id
    response = client.post(
        "/api/agent/runs/begin",
        json=body,
        headers=agent_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _event(
    run: dict[str, Any],
    runtime_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    include_correlation: bool = True,
    parent_run_id: str | None = None,
    include_parent: bool = False,
    trace_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id or str(uuid.uuid4()),
        "type": event_type,
        "occurred_at": "2026-08-24T01:02:00Z",
        "agent_id": "demo-agent",
        "run_id": run["run_id"],
        "severity": "info",
        "payload": payload,
    }
    if runtime_id is not None:
        event["runtime_instance_id"] = runtime_id
    if include_correlation:
        event["correlation_id"] = run["correlation_id"]
    if include_parent:
        event["parent_run_id"] = parent_run_id
    if trace_id is not None:
        event["trace_id"] = trace_id
    return event


def _post_events(client: TestClient, events: list[dict[str, Any]]) -> Any:
    return client.post(
        "/api/agent/events/batch",
        json={"events": events},
        headers=agent_headers(),
    )


def test_ack_rejections_leave_run_unchanged(client: TestClient) -> None:
    runtime_id = _register(client)
    run = _begin(client, runtime_id=runtime_id)
    endpoint = f"/api/agent/runs/{run['run_id']}/ack"

    missing = client.post(endpoint, headers=agent_headers())
    assert missing.status_code == 422
    mismatch = client.post(
        endpoint,
        json={"runtime_instance_id": str(uuid.uuid4()), "status": "running"},
        headers=agent_headers(),
    )
    assert mismatch.status_code == 409
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "running"

    completion = _event(
        run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"answer": 42}},
    )
    assert _post_events(client, [completion]).status_code == 202
    terminal = client.post(
        endpoint,
        json={"runtime_instance_id": runtime_id, "status": "running"},
        headers=agent_headers(),
    )
    assert terminal.status_code == 409
    stored = client.get(f"/api/runs/{run['run_id']}").json()
    assert stored["status"] == "succeeded"
    assert stored["output"] == {"answer": 42}


def test_event_requires_exact_run_assignment_and_lineage(client: TestClient) -> None:
    first_runtime = _register(client)
    second_runtime = _register(client, str(uuid.uuid4()))
    run = _begin(client, runtime_id=second_runtime)
    assigned_runtime = client.get(f"/api/runs/{run['run_id']}").json()["runtime_instance_id"]
    wrong_runtime = first_runtime if assigned_runtime == second_runtime else second_runtime

    missing_runtime = _event(run, None, "kitsune.run.started", {})
    assert _post_events(client, [missing_runtime]).status_code == 422
    wrong_assignment = _event(run, wrong_runtime, "kitsune.run.started", {})
    assert _post_events(client, [wrong_assignment]).status_code == 422
    wrong_parent = _event(
        run,
        assigned_runtime,
        "kitsune.run.started",
        {},
        parent_run_id=str(uuid.uuid4()),
        include_parent=True,
    )
    assert _post_events(client, [wrong_parent]).status_code == 422
    missing_correlation = _event(
        run,
        assigned_runtime,
        "kitsune.run.started",
        {},
        include_correlation=False,
    )
    assert _post_events(client, [missing_correlation]).status_code == 422

    first_trace = _event(
        run,
        assigned_runtime,
        "kitsune.run.started",
        {},
        trace_id="0123456789abcdef",
    )
    assert _post_events(client, [first_trace]).status_code == 202
    omitted_trace = _event(run, assigned_runtime, "kitsune.run.started", {})
    assert _post_events(client, [omitted_trace]).status_code == 422
    wrong_trace = _event(
        run,
        assigned_runtime,
        "kitsune.run.started",
        {},
        trace_id="fedcba9876543210",
    )
    assert _post_events(client, [wrong_trace]).status_code == 422
    stored = client.get(f"/api/runs/{run['run_id']}").json()
    assert stored["runtime_instance_id"] == assigned_runtime
    assert stored["trace_id"] == "0123456789abcdef"


def test_event_batch_rejects_conflicting_initial_trace_ids(client: TestClient) -> None:
    """A Run without a trace cannot acquire two different trace IDs atomically."""

    runtime_id = _register(client)
    run = _begin(client, runtime_id=runtime_id)
    first = _event(
        run,
        runtime_id,
        "kitsune.run.started",
        {},
        trace_id="0123456789abcdef",
    )
    second = _event(
        run,
        runtime_id,
        "kitsune.custom.progress",
        {},
        trace_id="fedcba9876543210",
    )

    response = _post_events(client, [first, second])

    assert response.status_code == 422
    with client.app.state.database.session() as session:
        stored_run = session.get(Run, run["run_id"])
        assert stored_run is not None and stored_run.trace_id is None
        assert session.get(Event, first["event_id"]) is None
        assert session.get(Event, second["event_id"]) is None


def test_late_terminal_events_are_persisted_without_mutating_run(client: TestClient) -> None:
    runtime_id = _register(client)
    timed_out_run = _begin(client)
    timeout = _event(timed_out_run, runtime_id, "kitsune.run.timed_out", {})
    late_success_id = str(uuid.uuid4())
    late_success = _event(
        timed_out_run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"late": True}},
        event_id=late_success_id,
    )
    assert _post_events(client, [timeout, late_success]).status_code == 202
    timed_out = client.get(f"/api/runs/{timed_out_run['run_id']}").json()
    assert timed_out["status"] == "timed_out"
    assert timed_out["output"] is None

    succeeded_run = _begin(client)
    success = _event(
        succeeded_run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"stable": True}},
    )
    late_failure_id = str(uuid.uuid4())
    late_failure = _event(
        succeeded_run,
        runtime_id,
        "kitsune.run.failed",
        {"error": {"type": "late", "message": "must not replace the result"}},
        event_id=late_failure_id,
    )
    assert _post_events(client, [success, late_failure]).status_code == 202
    succeeded = client.get(f"/api/runs/{succeeded_run['run_id']}").json()
    assert succeeded["status"] == "succeeded"
    assert succeeded["output"] == {"stable": True}
    assert succeeded["error"] is None
    with client.app.state.database.session() as session:
        assert session.get(Event, late_success_id) is not None
        assert session.get(Event, late_failure_id) is not None


def test_output_policy_never_persists_rejected_raw_output(client: TestClient) -> None:
    runtime_id = _register(client)
    client.app.state.settings.events.max_output_bytes = 1024
    oversized_run = _begin(client)
    raw_output = "sensitive-output-" + ("x" * 1100)
    oversized_id = str(uuid.uuid4())
    completion = _event(
        oversized_run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": raw_output},
        event_id=oversized_id,
    )
    completion["occurred_at"] = "2099-01-01T00:00:00Z"
    usage = _event(
        oversized_run,
        runtime_id,
        "kitsune.model.usage",
        {
            "provider": "test",
            "model": "test-model",
            "input_tokens": 2,
            "output_tokens": 1,
            "total_tokens": 3,
        },
    )
    usage["occurred_at"] = "2099-01-01T00:00:01Z"
    response = _post_events(client, [completion, usage])
    assert response.status_code == 202
    assert response.json()["accepted"] == 2
    oversized = client.get(f"/api/runs/{oversized_run['run_id']}").json()
    assert oversized["status"] == "failed"
    assert oversized["output"] is None
    assert oversized["error"]["type"] == "output_too_large"
    assert oversized["usage"][0]["total_tokens"] == 3
    with client.app.state.database.session() as session:
        stored_event = session.get(Event, oversized_id)
        stored_usage_event = session.get(Event, usage["event_id"])
        stored_run = session.get(Run, oversized_run["run_id"])
        stored_usage = session.query(UsageRecord).filter_by(event_id=usage["event_id"]).one()
        assert stored_event is not None
        assert stored_usage_event is not None
        assert stored_run is not None
        assert stored_run.ended_at == stored_event.received_at
        assert stored_run.ended_at != stored_event.occurred_at
        assert stored_usage.recorded_at == stored_usage_event.received_at
        assert stored_usage.recorded_at != stored_usage_event.occurred_at
        assert "output" not in stored_event.payload
        assert stored_event.payload["output_rejected"]["reason"] == "too_large"
        assert raw_output not in json.dumps(stored_event.payload)

        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["invocation"]["store_output"] = False
        definition.snapshot = snapshot
    omitted_run = _begin(client)
    omitted_id = str(uuid.uuid4())
    omitted = _event(
        omitted_run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"must": "not persist"}},
        event_id=omitted_id,
    )
    assert _post_events(client, [omitted]).status_code == 202
    without_output = client.get(f"/api/runs/{omitted_run['run_id']}").json()
    assert without_output["status"] == "succeeded"
    assert without_output["output"] is None
    with client.app.state.database.session() as session:
        stored_event = session.get(Event, omitted_id)
        assert stored_event is not None
        assert "output" not in stored_event.payload
        assert stored_event.payload["output_omitted"] == {"reason": "persistence_disabled"}


def test_event_payload_policy_omits_nested_run_content_and_preserves_default(
    client: TestClient,
) -> None:
    runtime_id = _register(client)
    with client.app.state.database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["invocation"]["max_concurrency"] = 3
        snapshot["spec"]["invocation"]["store_input"] = False
        snapshot["spec"]["invocation"]["store_output"] = False
        definition.snapshot = snapshot

    private_run = _begin(client)
    private_event_id = str(uuid.uuid4())
    nested_content = {
        "phase": "tool-finished",
        "text": "private-unknown-text",
        "input": {"prompt": "private-prompt"},
        "nested": {
            "value": "private-unknown-value",
            "inputs": ["private-input"],
            "output": {"tool_result": "private-tool-result"},
            "results": [{"value": "private-output"}],
        },
    }
    private_event = _event(
        private_run,
        runtime_id,
        "kitsune.custom.progress",
        nested_content,
        event_id=private_event_id,
    )
    private_usage_id = str(uuid.uuid4())
    private_usage = _event(
        private_run,
        runtime_id,
        "kitsune.model.usage",
        {
            "provider": "test",
            "model": "safe-model-id",
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
        },
        event_id=private_usage_id,
    )
    response = _post_events(client, [private_event, private_usage])
    assert response.status_code == 202
    assert response.json()["accepted"] == 2

    with client.app.state.database.session() as session:
        stored_private = session.get(Event, private_event_id)
        stored_usage_event = session.get(Event, private_usage_id)
        assert stored_private is not None
        assert stored_usage_event is not None
        assert stored_usage_event.payload == {
            "provider": "test",
            "model": "safe-model-id",
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
        }
        assert stored_private.payload["phase"] == "tool-finished"
        assert stored_private.payload["input_omitted"] == {"reason": "persistence_disabled"}
        assert stored_private.payload["output_omitted"] == {"reason": "persistence_disabled"}
        assert "text" not in stored_private.payload
        assert "nested" not in stored_private.payload
        serialized = json.dumps(stored_private.payload)
        for private_value in (
            "private-unknown-text",
            "private-unknown-value",
            "private-prompt",
            "private-input",
            "private-tool-result",
            "private-output",
        ):
            assert private_value not in serialized

        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["invocation"]["store_input"] = False
        snapshot["spec"]["invocation"]["store_output"] = True
        definition.snapshot = snapshot

    output_run = _begin(client)
    output_event_id = str(uuid.uuid4())
    output_event = _event(
        output_run,
        runtime_id,
        "kitsune.custom.progress",
        {
            "phase": "output-ready",
            "input": {"value": "disabled-input"},
            "output": {"value": "enabled-output"},
            "text": "unknown-content",
        },
        event_id=output_event_id,
    )
    assert _post_events(client, [output_event]).status_code == 202
    with client.app.state.database.session() as session:
        stored_output = session.get(Event, output_event_id)
        assert stored_output is not None
        assert stored_output.payload["phase"] == "output-ready"
        assert stored_output.payload["output"] == {"value": "enabled-output"}
        assert stored_output.payload["input_omitted"] == {"reason": "persistence_disabled"}
        serialized = json.dumps(stored_output.payload)
        assert "disabled-input" not in serialized
        assert "unknown-content" not in serialized

        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["invocation"]["store_input"] = True
        snapshot["spec"]["invocation"]["store_output"] = True
        definition.snapshot = snapshot

    default_run = _begin(client)
    default_event_id = str(uuid.uuid4())
    default_event = _event(
        default_run,
        runtime_id,
        "kitsune.custom.progress",
        nested_content,
        event_id=default_event_id,
    )
    assert _post_events(client, [default_event]).status_code == 202
    with client.app.state.database.session() as session:
        stored_default = session.get(Event, default_event_id)
        assert stored_default is not None
        assert stored_default.payload == nested_content


def test_ready_events_use_receipt_time_and_cannot_resurrect_terminal_runtimes(
    client: TestClient,
) -> None:
    active_id = _register(client, str(uuid.uuid4()))
    stopped_id = _register(client, str(uuid.uuid4()))
    lost_id = _register(client, str(uuid.uuid4()))
    with client.app.state.database.session() as session:
        active = session.get(RuntimeInstance, active_id)
        stopped = session.get(RuntimeInstance, stopped_id)
        lost = session.get(RuntimeInstance, lost_id)
        assert active is not None and stopped is not None and lost is not None
        active.status = "starting"
        active.ready_at = None
        active.last_heartbeat_at = None
        stopped.status = "stopped"
        lost.status = "lost"

    active_event_id = str(uuid.uuid4())
    future_occurred_at = "2099-01-01T00:00:00Z"
    active_event = {
        "event_id": active_event_id,
        "type": "kitsune.runtime.ready",
        "occurred_at": future_occurred_at,
        "agent_id": "demo-agent",
        "runtime_instance_id": active_id,
        "severity": "info",
        "payload": {},
    }
    stopped_event = {
        **active_event,
        "event_id": str(uuid.uuid4()),
        "runtime_instance_id": stopped_id,
    }
    lost_event = {
        **active_event,
        "event_id": str(uuid.uuid4()),
        "runtime_instance_id": lost_id,
        "occurred_at": "2000-01-01T00:00:00Z",
    }
    response = _post_events(client, [active_event, stopped_event, lost_event])
    assert response.status_code == 202
    assert response.json()["accepted"] == 3

    with client.app.state.database.session() as session:
        active = session.get(RuntimeInstance, active_id)
        stopped = session.get(RuntimeInstance, stopped_id)
        lost = session.get(RuntimeInstance, lost_id)
        stored_event = session.get(Event, active_event_id)
        assert active is not None and stopped is not None and lost is not None
        assert stored_event is not None
        assert active.status == "ready"
        assert active.ready_at == stored_event.received_at
        assert active.last_heartbeat_at == stored_event.received_at
        assert active.ready_at != stored_event.occurred_at
        assert stopped.status == "stopped"
        assert lost.status == "lost"
        first_heartbeat = active.last_heartbeat_at

    replay = _post_events(client, [active_event])
    assert replay.status_code == 202
    assert replay.json() == {"accepted": 0, "duplicates": [active_event_id]}
    with client.app.state.database.session() as session:
        active = session.get(RuntimeInstance, active_id)
        assert active is not None
        assert active.last_heartbeat_at == first_heartbeat

    retention_at = first_heartbeat.replace(tzinfo=UTC) + timedelta(days=31)
    result = client.app.state.control.retention.run(retention_at)
    assert result["events"] >= 3
    with client.app.state.database.session() as session:
        assert session.get(Event, active_event_id) is None


@pytest.mark.parametrize(
    "invalid_usage",
    [
        {"input_tokens": -1},
        {"estimated_cost": 0.5},
        {"input_tokens": 1, "unexpected": True},
    ],
)
def test_invalid_usage_rejects_entire_batch(
    client: TestClient, invalid_usage: dict[str, Any]
) -> None:
    runtime_id = _register(client)
    run = _begin(client)
    completion_id = str(uuid.uuid4())
    completion = _event(
        run,
        runtime_id,
        "kitsune.run.succeeded",
        {"output": {"must": "roll back"}},
        event_id=completion_id,
    )
    usage = _event(
        run,
        runtime_id,
        "kitsune.model.usage",
        invalid_usage,
    )
    response = _post_events(client, [completion, usage])
    assert response.status_code == 422
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "running"
    with client.app.state.database.session() as session:
        assert session.get(Event, completion_id) is None


def test_run_service_enforces_lineage_and_inherits_parent_context(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory("agent-one")
    manifest_factory("agent-two")
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    try:
        parent, _ = control.runs.create(
            agent_id="agent-one",
            handler="default",
            source="self",
            input_value={},
            timeout_seconds=10,
        )
        with pytest.raises(RunServiceError, match="requires parent_run_id"):
            control.runs.create(
                agent_id="agent-two",
                handler="default",
                source="child",
                input_value={},
            )
        with pytest.raises(RunServiceError, match="only valid for a child"):
            control.runs.create(
                agent_id="agent-two",
                handler="default",
                source="self",
                parent_run_id=parent.run_id,
                input_value={},
            )
        with pytest.raises(RunServiceError, match="correlation_id"):
            control.runs.create(
                agent_id="agent-two",
                handler="default",
                source="child",
                parent_run_id=parent.run_id,
                correlation_id=str(uuid.uuid4()),
                input_value={},
            )
        child, _ = control.runs.create(
            agent_id="agent-two",
            handler="default",
            source="child",
            parent_run_id=parent.run_id,
            input_value={},
            timeout_seconds=60,
        )
        assert child.parent_run_id == parent.run_id
        assert child.agent_id != parent.agent_id
        assert child.correlation_id == parent.correlation_id
        assert child.deadline is not None and parent.deadline is not None
        assert child.deadline <= parent.deadline + timedelta(microseconds=1)
    finally:
        control.telemetry.shutdown()
        database.dispose()


def test_agent_run_begin_contract_rejects_invalid_lineage(client: TestClient) -> None:
    _register(client)
    base = {
        "run_id": str(uuid.uuid4()),
        "agent_id": "demo-agent",
        "runtime_instance_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "runtime.demo-agent")),
        "handler": "default",
        "correlation_id": str(uuid.uuid4()),
        "input": {},
    }
    child_without_parent = client.post(
        "/api/agent/runs/begin",
        json={**base, "source": "child"},
        headers=agent_headers(),
    )
    assert child_without_parent.status_code == 422
    self_with_parent = client.post(
        "/api/agent/runs/begin",
        json={
            **base,
            "run_id": str(uuid.uuid4()),
            "source": "self",
            "parent_run_id": str(uuid.uuid4()),
        },
        headers=agent_headers(),
    )
    assert self_with_parent.status_code == 422


@pytest.mark.parametrize(
    ("max_concurrency", "handler_limit", "expected_detail"),
    [
        (1, None, "Agent concurrency is exhausted"),
        (2, 1, "Handler 'default' concurrency is exhausted"),
    ],
)
def test_agent_run_begin_cannot_consume_queue_capacity_as_concurrency(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    max_concurrency: int,
    handler_limit: int | None,
    expected_detail: str,
) -> None:
    manifest_factory(max_concurrency=max_concurrency, queue_capacity=20)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        runtime_id = _register(client)
        if handler_limit is not None:
            with client.app.state.database.session() as session:
                handler = (
                    session.query(Handler).filter_by(agent_id="demo-agent", name="default").one()
                )
                handler.max_concurrency = handler_limit
        base = {
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "handler": "default",
            "source": "self",
            "input": {},
        }
        first = client.post(
            "/api/agent/runs/begin",
            json={
                **base,
                "run_id": str(uuid.uuid4()),
                "correlation_id": str(uuid.uuid4()),
            },
            headers=agent_headers(),
        )
        rejected = client.post(
            "/api/agent/runs/begin",
            json={
                **base,
                "run_id": str(uuid.uuid4()),
                "correlation_id": str(uuid.uuid4()),
            },
            headers=agent_headers(),
        )

        assert first.status_code == 201, first.text
        assert rejected.status_code == 429
        assert rejected.json()["detail"] == expected_detail


def test_agent_run_begin_uses_exact_ready_runtime_for_self_and_child(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(max_concurrency=2, queue_capacity=2)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        first_runtime = _register(client)
        second_runtime = _register(client, str(uuid.uuid4()))
        correlation_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())
        parent = client.post(
            "/api/agent/runs/begin",
            json={
                "run_id": parent_id,
                "agent_id": "demo-agent",
                "runtime_instance_id": first_runtime,
                "handler": "default",
                "source": "self",
                "correlation_id": correlation_id,
                "input": {},
            },
            headers=agent_headers(),
        )
        assert parent.status_code == 201, parent.text
        child_id = str(uuid.uuid4())
        child = client.post(
            "/api/agent/runs/begin",
            json={
                "run_id": child_id,
                "agent_id": "demo-agent",
                "runtime_instance_id": second_runtime,
                "handler": "child-operation",
                "source": "child",
                "parent_run_id": parent_id,
                "correlation_id": correlation_id,
                "input": {},
            },
            headers=agent_headers(),
        )
        assert child.status_code == 201, child.text
        assert client.get(f"/api/runs/{parent_id}").json()["runtime_instance_id"] == first_runtime
        child_view = client.get(f"/api/runs/{child_id}").json()
        assert child_view["runtime_instance_id"] == second_runtime
        assert child_view["parent_run_id"] == parent_id
        assert child_view["handler"] == "child-operation"

        forged = client.post(
            "/api/agent/runs/begin",
            json={
                "run_id": str(uuid.uuid4()),
                "agent_id": "demo-agent",
                "runtime_instance_id": str(uuid.uuid4()),
                "handler": "default",
                "source": "self",
                "correlation_id": str(uuid.uuid4()),
                "input": {},
            },
            headers=agent_headers(),
        )
        assert forged.status_code == 422

        with client.app.state.database.session() as session:
            instance = session.get(RuntimeInstance, first_runtime)
            assert instance is not None
            instance.status = "unhealthy"
        not_ready = client.post(
            "/api/agent/runs/begin",
            json={
                "run_id": str(uuid.uuid4()),
                "agent_id": "demo-agent",
                "runtime_instance_id": first_runtime,
                "handler": "default",
                "source": "self",
                "correlation_id": str(uuid.uuid4()),
                "input": {},
            },
            headers=agent_headers(),
        )
        assert not_ready.status_code == 422


def test_external_ephemeral_registers_without_resident_control_url(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    path = manifest_factory(adapter="external", mode="ephemeral")
    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "    external:\n"
        "      endpoint: https://agent.example.invalid\n"
        "      heartbeat_timeout_seconds: 30\n",
        "    external: {}\n",
    )
    path.write_text(content, encoding="utf-8")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        runtime_id = str(uuid.uuid4())
        body = registration(runtime_id=runtime_id)
        body.pop("control_url")
        registered = client.post(
            "/api/agent/register",
            json=body,
            headers=agent_headers(),
        )
        assert registered.status_code == 201, registered.text
        run_id = str(uuid.uuid4())
        begun = client.post(
            "/api/agent/runs/begin",
            json={
                "run_id": run_id,
                "agent_id": "demo-agent",
                "runtime_instance_id": runtime_id,
                "handler": "default",
                "source": "self",
                "correlation_id": str(uuid.uuid4()),
                "input": {},
            },
            headers=agent_headers(),
        )
        assert begun.status_code == 201, begun.text
        assignment = begun.json()
        completed = _post_events(
            client,
            [
                _event(
                    assignment,
                    runtime_id,
                    "kitsune.run.succeeded",
                    {"output": {"external": "ephemeral"}},
                )
            ],
        )
        assert completed.status_code == 202, completed.text
        stored = client.get(f"/api/runs/{run_id}").json()
        assert stored["status"] == "succeeded"
        assert stored["output"] == {"external": "ephemeral"}
