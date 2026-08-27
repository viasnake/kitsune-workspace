"""Agent wire contract, token, event deduplication, scope, webhook, and audit tests."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import ManifestFactory, agent_headers, registration, settings_for
from fastapi.testclient import TestClient

from kitsune_workspace.app import create_app
from kitsune_workspace.database import Database, InstanceLock
from kitsune_workspace.models import (
    AgentCredential,
    AgentDefinition,
    AgentDescriptor,
    Event,
    Handler,
    RuntimeInstance,
    WorkspaceLock,
)
from kitsune_workspace.util import ensure_aware, utcnow


def _register(client: TestClient, agent_id: str = "demo-agent") -> str:
    response = client.post(
        "/api/agent/register",
        json=registration(agent_id),
        headers=agent_headers(agent_id),
    )
    assert response.status_code == 201, response.text
    runtime_id = response.json()["runtime_instance_id"]
    with client.app.state.database.session() as session:
        from kitsune_workspace.models import RuntimeInstance

        instance = session.get(RuntimeInstance, runtime_id)
        assert instance is not None
        instance.status = "ready"
        instance.ready_at = instance.ready_at or instance.started_at
    return runtime_id


def _begin(client: TestClient, agent_id: str = "demo-agent") -> dict[str, object]:
    run_id = str(uuid.uuid4())
    runtime_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"runtime.{agent_id}"))
    response = client.post(
        "/api/agent/runs/begin",
        json={
            "run_id": run_id,
            "agent_id": agent_id,
            "runtime_instance_id": runtime_id,
            "handler": "default",
            "source": "self",
            "parent_run_id": None,
            "correlation_id": str(uuid.uuid4()),
            "input": {"message": "hello"},
        },
        headers=agent_headers(agent_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_sdk_registration_heartbeat_begin_ack_and_input(client: TestClient) -> None:
    runtime_id = _register(client)
    heartbeat = client.post(
        "/api/agent/heartbeat",
        json={
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": "2026-08-24T01:00:00Z",
            "status": "ready",
            "active_runs": 0,
        },
        headers=agent_headers(),
    )
    assert heartbeat.status_code == 200
    run = _begin(client)
    assignment = client.get(f"/api/agent/runs/{run['run_id']}/input", headers=agent_headers())
    assert assignment.status_code == 200
    assert assignment.json()["source"] == "self"
    assert assignment.json()["correlation_id"] == run["correlation_id"]
    acknowledgement = client.post(
        f"/api/agent/runs/{run['run_id']}/ack",
        json={"runtime_instance_id": runtime_id, "status": "running"},
        headers=agent_headers(),
    )
    assert acknowledgement.status_code == 200
    assert acknowledgement.json()["status"] == "running"


def test_heartbeat_liveness_uses_server_receipt_time(client: TestClient) -> None:
    runtime_id = _register(client)
    before = utcnow()
    response = client.post(
        "/api/agent/heartbeat",
        json={
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": (before + timedelta(days=3650)).isoformat(),
            "status": "ready",
            "active_runs": 0,
        },
        headers=agent_headers(),
    )
    assert response.status_code == 200
    with client.app.state.database.session() as session:
        instance = session.get(RuntimeInstance, runtime_id)
        assert instance is not None and instance.last_heartbeat_at is not None
        received_at = ensure_aware(instance.last_heartbeat_at)
    assert before <= received_at <= utcnow()


def test_ephemeral_agent_rejects_resident_lifecycle_actions(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(adapter="process", mode="ephemeral", desired_state="stopped")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        for action in ("start", "stop", "restart"):
            response = client.post(f"/api/agents/demo-agent/{action}")
            assert response.status_code == 409, response.text
        with client.app.state.database.session() as session:
            definition = session.get(AgentDefinition, "demo-agent")
            assert definition is not None
            assert definition.desired_state == "stopped"
            assert session.query(RuntimeInstance).count() == 0


def test_parallel_registration_replaces_descriptor_atomically(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(adapter="external", mode="resident")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        body = registration()

        def register(_: int) -> int:
            return client.post(
                "/api/agent/register", json=body, headers=agent_headers()
            ).status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(register, range(8)))
        assert statuses == [201] * 8
        with client.app.state.database.session() as session:
            assert session.query(AgentDescriptor).count() == 1
            assert session.query(Handler).count() == 1


def test_post_sanitized_descriptor_limit_rejects_without_partial_replacement(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> None:
    """Secret replacement expansion is bounded before the descriptor mutation begins."""

    manifest_factory(adapter="external", mode="resident", **{"token": "x"})
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        body = registration()
        headers = {"Authorization": "Bearer x"}
        first = client.post("/api/agent/register", json=body, headers=headers)
        assert first.status_code == 201, first.text

        oversized = registration(runtime_id=body["runtime_instance_id"])
        oversized["descriptor"]["capabilities"] = {"values": ["x"] * 100_000}
        rejected = client.post("/api/agent/register", json=oversized, headers=headers)

        assert rejected.status_code == 422, rejected.text
        assert rejected.json() == {"detail": "Agent descriptor cannot be safely persisted"}
        with client.app.state.database.session() as session:
            stored = session.query(AgentDescriptor).one()
            handlers = session.query(Handler).all()
            assert stored.raw.get("capabilities", {}) == {}
            assert [handler.name for handler in handlers] == ["default"]


def test_expired_old_owner_rejects_http_mutation_after_takeover(client: TestClient) -> None:
    """The local HTTP gate closes at the last confirmed lease expiry, before renewal."""

    old_control = client.app.state.control
    old_control.instance_lock.confirmed_expires_at = utcnow() - timedelta(seconds=1)
    takeover_database = Database(old_control.settings.workspace.database_url)
    takeover = InstanceLock(
        takeover_database,
        old_control.instance_lock.name,
        old_control.settings.scheduler.lock_ttl_seconds,
    )
    try:
        with takeover_database.session(fence=False) as session:
            lock = session.get(WorkspaceLock, takeover.name)
            assert lock is not None
            lock.expires_at = utcnow() - timedelta(seconds=1)
        takeover.acquire()

        rejected = client.post("/api/agents/demo-agent/stop")

        assert rejected.status_code == 503, rejected.text
        assert rejected.json() == {"detail": "control-plane instance lock is not owned"}
        with takeover_database.session(fence=False) as session:
            definition = session.get(AgentDefinition, "demo-agent")
            assert definition is not None and definition.desired_state == "running"
    finally:
        takeover.release()
        takeover_database.dispose()


def test_transport_limit_honors_input_limit_larger_than_event_limit(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(adapter="external", mode="resident")
    settings = settings_for(tmp_path)
    settings.events.max_payload_bytes = 1_024
    settings.events.max_output_bytes = 1_024
    settings.events.max_input_bytes = 131_072
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        _register(client)
        response = client.post(
            "/api/agents/demo-agent/runs",
            json={"handler": "default", "input": {"value": "x" * 70_000}},
        )
        assert response.status_code == 202, response.text


def test_event_batch_deduplicates_records_usage_and_terminal_output(client: TestClient) -> None:
    runtime_id = _register(client)
    run = _begin(client)
    publish = AsyncMock()
    client.app.state.control.events.publish = publish
    correlation_id = run["correlation_id"]
    usage_id = str(uuid.uuid4())
    completion_id = str(uuid.uuid4())
    events = [
        {
            "event_id": usage_id,
            "type": "kitsune.model.usage",
            "occurred_at": "2026-08-24T01:01:00Z",
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "run_id": run["run_id"],
            "correlation_id": correlation_id,
            "severity": "info",
            "payload": {
                "provider": "test",
                "model": "fake",
                "request_count": 1,
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
            },
        },
        {
            "event_id": completion_id,
            "type": "kitsune.run.succeeded",
            "occurred_at": "2026-08-24T01:02:00Z",
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "run_id": run["run_id"],
            "correlation_id": correlation_id,
            "severity": "info",
            "payload": {"output": {"answer": 42}},
        },
    ]
    first = client.post("/api/agent/events/batch", json={"events": events}, headers=agent_headers())
    assert first.status_code == 202, first.text
    assert first.json() == {"accepted": 2, "duplicates": []}
    publish.assert_any_await(
        "event",
        {
            "event_id": usage_id,
            "type": "kitsune.model.usage",
            "agent_id": "demo-agent",
            "run_id": run["run_id"],
        },
    )
    publish.assert_any_await(
        "event",
        {
            "event_id": completion_id,
            "type": "kitsune.run.succeeded",
            "agent_id": "demo-agent",
            "run_id": run["run_id"],
        },
    )
    duplicate = client.post(
        "/api/agent/events/batch", json={"events": events}, headers=agent_headers()
    )
    assert duplicate.status_code == 202
    assert set(duplicate.json()["duplicates"]) == {usage_id, completion_id}
    stored = client.get(f"/api/runs/{run['run_id']}").json()
    assert stored["status"] == "succeeded"
    assert stored["output"] == {"answer": 42}
    assert stored["usage"][0]["total_tokens"] == 14


def test_agent_error_text_is_redacted_before_event_and_run_persistence(
    client: TestClient,
) -> None:
    """A compromised Agent cannot persist credentials inside unstructured error text."""

    secret = "workspace-ingest-sentinel"  # noqa: S105 - synthetic leak canary
    runtime_id = _register(client)
    run = _begin(client)
    event_id = str(uuid.uuid4())
    event = {
        "event_id": event_id,
        "type": "kitsune.run.failed",
        "occurred_at": "2026-08-24T01:03:00Z",
        "agent_id": "demo-agent",
        "runtime_instance_id": runtime_id,
        "run_id": run["run_id"],
        "correlation_id": run["correlation_id"],
        "severity": "error",
        "payload": {
            "error": {
                "type": "RuntimeError",
                "message": f"Authorization: Bearer {secret}",
                "retryable": False,
                "details": {"note": f"api_key={secret}"},
            }
        },
    }

    response = client.post(
        "/api/agent/events/batch",
        json={"events": [event]},
        headers=agent_headers(),
    )

    assert response.status_code == 202, response.text
    run_view = client.get(f"/api/runs/{run['run_id']}").json()
    event_views = client.get(f"/api/runs/{run['run_id']}/events").json()
    with client.app.state.database.session() as session:
        stored_event = session.get(Event, event_id)
        assert stored_event is not None
        stored_payload = stored_event.payload
    serialized = repr({"run": run_view, "events": event_views, "db": stored_payload})
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_event_payload_redacts_normalized_sensitive_key_components(client: TestClient) -> None:
    """EventService applies the mandatory matcher before its durable JSON boundary."""

    event_id = str(uuid.uuid4())
    secrets = {
        "access_token": "snake-event-secret",
        "refreshToken": "camel-event-secret",
        "private_key": "private-event-secret",
        "X-API-Key": "header-event-secret",
    }
    response = client.post(
        "/api/agent/events/batch",
        json={
            "events": [
                {
                    "event_id": event_id,
                    "type": "kitsune.security.redaction",
                    "occurred_at": "2026-08-24T01:03:15Z",
                    "agent_id": "demo-agent",
                    "severity": "info",
                    "payload": {
                        **secrets,
                        "message": (
                            "access_token=snake-text-secret "
                            "refreshToken=camel-text-secret "
                            "privateKey=private-text-secret "
                            "X-API-Key: header-text-secret"
                        ),
                        "safe_monkey": "visible",
                    },
                }
            ]
        },
        headers=agent_headers(),
    )

    assert response.status_code == 202, response.text
    with client.app.state.database.session() as session:
        stored = session.get(Event, event_id)
        assert stored is not None
        payload = stored.payload
    serialized = repr(payload)
    for secret in (
        *secrets.values(),
        "snake-text-secret",
        "camel-text-secret",
        "private-text-secret",
        "header-text-secret",
    ):
        assert secret not in serialized
    assert payload["access_token"] == "[REDACTED]"  # noqa: S105
    assert payload["refreshToken"] == "[REDACTED]"
    assert payload["private_key"] == "[REDACTED]"
    assert payload["X-API-Key"] == "[REDACTED]"
    assert payload["safe_monkey"] == "visible"
    assert payload["message"].count("[REDACTED]") == 4


def test_issued_agent_bearer_is_redacted_as_an_ephemeral_request_secret(
    client: TestClient,
) -> None:
    """The plaintext issued token authenticates one request but never enters Run/Event rows."""

    runtime_id = _register(client)
    run = _begin(client)
    issued = client.post(
        "/api/admin/tokens",
        json={"agent_id": "demo-agent", "description": "persistence privacy test"},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    assert token.startswith("kt_agent_")
    event_id = str(uuid.uuid4())
    event = {
        "event_id": event_id,
        "type": "kitsune.run.failed",
        "occurred_at": "2026-08-24T01:03:30Z",
        "agent_id": "demo-agent",
        "runtime_instance_id": runtime_id,
        "run_id": run["run_id"],
        "correlation_id": run["correlation_id"],
        "severity": "error",
        "payload": {
            "error": {
                "type": "RuntimeError",
                "message": f"provider returned the bare credential {token}",
                "retryable": False,
                "details": {"echo": token},
            }
        },
    }

    response = client.post(
        "/api/agent/events/batch",
        json={"events": [event]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202, response.text
    run_view = client.get(f"/api/runs/{run['run_id']}").json()
    event_views = client.get(f"/api/runs/{run['run_id']}/events").json()
    with client.app.state.database.session() as session:
        stored_event = session.get(Event, event_id)
        assert stored_event is not None
        stored_payload = stored_event.payload
    serialized = repr({"run": run_view, "events": event_views, "db": stored_payload})
    assert token not in serialized
    assert "[REDACTED]" in serialized


def test_registration_redacts_issued_bearer_and_revalidates_structural_fields(
    client: TestClient,
) -> None:
    """Registration persists a valid sanitized descriptor, never its request credential."""

    runtime_id = _register(client)
    issued = client.post(
        "/api/admin/tokens",
        json={"agent_id": "demo-agent", "description": "registration privacy test"},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    assert token.startswith("kt_agent_")
    headers = {"Authorization": f"Bearer {token}"}

    structurally_sensitive = registration(runtime_id=runtime_id)
    structurally_sensitive["descriptor"]["handlers"][0]["name"] = token
    rejected = client.post(
        "/api/agent/register",
        json=structurally_sensitive,
        headers=headers,
    )

    assert rejected.status_code == 422, rejected.text
    assert token not in rejected.text
    with client.app.state.database.session() as session:
        assert session.query(AgentDescriptor).one().raw["handlers"][0]["name"] == "default"
        assert session.query(Handler).one().name == "default"

    bootstrap_secret = "secret-demo-agent"  # noqa: S105 - fixture bootstrap credential
    unknown_password = "unregistered-capability-password"  # noqa: S105
    unknown_authorization = "unregistered-capability-authorization"  # noqa: S105
    unknown_schema_secret = "unregistered-schema-access-token"  # noqa: S105
    body = registration(runtime_id=str(uuid.uuid4()))
    descriptor = body["descriptor"]
    descriptor["application_version"] = f"release authenticated with {token}"
    descriptor["sdk_version"] = f"sdk configured with {bootstrap_secret}"
    descriptor["framework"] = f"generic token={token}"
    descriptor["build_revision"] = token
    descriptor["plugins"] = [{"name": "test-plugin", "version": token}]
    descriptor["capabilities"] = {
        "password": unknown_password,
        "authorization": unknown_authorization,
        "note": f"credential echoed as free text: {token}",
    }
    descriptor["handlers"][0]["description"] = (
        f"handler received {token} and bootstrap {bootstrap_secret}"
    )
    descriptor["handlers"][0]["input_schema"] = {
        "type": "object",
        "properties": {
            "password": {"type": "string", "default": token},
            "access_token": {"type": "string", "default": unknown_schema_secret},
            "safe_field": {"type": "string", "default": token},
        },
    }
    descriptor["handlers"][0]["output_schema"] = {
        "type": "object",
        "default": {"api_key": bootstrap_secret},
    }

    accepted = client.post("/api/agent/register", json=body, headers=headers)
    detail = client.get("/api/agents/demo-agent")

    assert accepted.status_code == 201, accepted.text
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    persisted_schema = detail_body["handlers"][0]["input_schema"]
    assert persisted_schema["properties"]["password"] == {
        "type": "string",
        "default": "[REDACTED]",  # noqa: S105
    }
    assert persisted_schema["properties"]["access_token"] == {
        "type": "string",
        "default": "[REDACTED]",  # noqa: S105
    }
    assert persisted_schema["properties"]["safe_field"]["default"] == "[REDACTED]"
    with client.app.state.database.session() as session:
        stored_descriptor = session.query(AgentDescriptor).one()
        stored_handlers = list(
            session.query(Handler).filter(Handler.agent_id == "demo-agent").all()
        )
        persisted = {
            "application_version": stored_descriptor.application_version,
            "sdk_version": stored_descriptor.sdk_version,
            "framework": stored_descriptor.framework,
            "build_revision": stored_descriptor.build_revision,
            "plugins": stored_descriptor.plugins,
            "capabilities": stored_descriptor.capabilities,
            "raw": stored_descriptor.raw,
            "handlers": [
                {
                    "name": handler.name,
                    "description": handler.description,
                    "input_schema": handler.input_schema,
                    "output_schema": handler.output_schema,
                    "default_timeout_seconds": handler.default_timeout_seconds,
                }
                for handler in stored_handlers
            ],
        }
    serialized = repr(
        {
            "registration": accepted.json(),
            "detail": detail_body,
            "database": persisted,
        }
    )
    assert token not in serialized
    assert bootstrap_secret not in serialized
    assert unknown_password not in serialized
    assert unknown_authorization not in serialized
    assert unknown_schema_secret not in serialized
    assert "[REDACTED]" in serialized


def test_run_input_redacts_resolved_manifest_bootstrap_secret(client: TestClient) -> None:
    """Management, DB, and Agent assignment views never expose a known Manifest secret."""

    secret = "secret-demo-agent"  # noqa: S105 - fixture bootstrap credential
    response = client.post(
        "/api/agents/demo-agent/runs",
        json={
            "handler": "default",
            "input": {
                "note": f"caller accidentally included {secret}",
                "authorization": secret,
            },
        },
    )

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    run_view = client.get(f"/api/runs/{run_id}").json()
    with client.app.state.database.session() as session:
        from kitsune_workspace.models import Run

        stored = session.get(Run, run_id)
        assert stored is not None
        stored_input = stored.input
    serialized = repr({"response": response.json(), "api": run_view, "db": stored_input})
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_agent_begin_redacts_issued_bearer_and_hashes_idempotency_key(
    client: TestClient,
) -> None:
    """Agent-originated input/key dedupe without retaining the presented credential."""

    runtime_id = _register(client)
    issued = client.post(
        "/api/admin/tokens",
        json={"agent_id": "demo-agent", "description": "begin privacy test"},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    first_run_id = str(uuid.uuid4())
    request = {
        "run_id": first_run_id,
        "agent_id": "demo-agent",
        "runtime_instance_id": runtime_id,
        "handler": "default",
        "source": "self",
        "parent_run_id": None,
        "correlation_id": str(uuid.uuid4()),
        "input": {"bare_credential": token},
        "idempotency_key": token,
    }
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/api/agent/runs/begin", json=request, headers=headers)
    duplicate = client.post(
        "/api/agent/runs/begin",
        json={**request, "run_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["run_id"] == first_run_id
    with client.app.state.database.session() as session:
        from kitsune_workspace.models import Run

        stored = session.get(Run, first_run_id)
        assert stored is not None
        assert stored.idempotency_key is not None
        assert len(stored.idempotency_key) == 64
        persisted = {"input": stored.input, "idempotency_key": stored.idempotency_key}
    serialized = repr({"response": first.json(), "db": persisted})
    assert token not in serialized
    assert "[REDACTED]" in serialized


def test_usage_metrics_keep_untrusted_provider_and_model_out_of_attributes(
    client: TestClient,
) -> None:
    """Usage dimensions stay durable without creating attacker-controlled metric series."""

    class CaptureCounter:
        def __init__(self) -> None:
            self.attributes: list[dict[str, object]] = []

        def add(self, _: int | float, attributes: dict[str, object]) -> None:
            self.attributes.append(dict(attributes))

    runtime_id = _register(client)
    run = _begin(client)
    counters = [CaptureCounter() for _ in range(4)]
    telemetry = client.app.state.control.telemetry
    (
        telemetry.model_requests,
        telemetry.model_input_tokens,
        telemetry.model_output_tokens,
        telemetry.model_estimated_cost,
    ) = counters
    events = []
    for index in range(8):
        events.append(
            {
                "event_id": str(uuid.uuid4()),
                "type": "kitsune.model.usage",
                "occurred_at": f"2026-08-24T01:04:{index:02d}Z",
                "agent_id": "demo-agent",
                "runtime_instance_id": runtime_id,
                "run_id": run["run_id"],
                "correlation_id": run["correlation_id"],
                "severity": "info",
                "payload": {
                    "provider": f"attacker-provider-{index}",
                    "model": f"attacker-model-{index}",
                    "request_count": 1,
                    "input_tokens": index + 1,
                    "output_tokens": index + 2,
                    "total_tokens": index * 2 + 3,
                    "estimated_cost": "0.01",
                    "currency": "USD",
                },
            }
        )

    response = client.post(
        "/api/agent/events/batch",
        json={"events": events},
        headers=agent_headers(),
    )

    assert response.status_code == 202, response.text
    for counter in counters:
        assert counter.attributes == [{"agent_id": "demo-agent"}] * len(events)
    usage = client.get(f"/api/runs/{run['run_id']}/usage").json()
    assert {item["provider"] for item in usage} == {
        f"attacker-provider-{index}" for index in range(8)
    }
    assert {item["model"] for item in usage} == {f"attacker-model-{index}" for index in range(8)}


def test_event_cannot_mutate_another_agents_run(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory("agent-one")
    manifest_factory("agent-two")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        runtime_one = _register(client, "agent-one")
        _register(client, "agent-two")
        run_two = _begin(client, "agent-two")
        event = {
            "event_id": str(uuid.uuid4()),
            "type": "kitsune.run.succeeded",
            "occurred_at": "2026-08-24T01:02:00Z",
            "agent_id": "agent-one",
            "runtime_instance_id": runtime_one,
            "run_id": run_two["run_id"],
            "correlation_id": run_two["correlation_id"],
            "severity": "info",
            "payload": {"output": {"poisoned": True}},
        }
        response = client.post(
            "/api/agent/events/batch",
            json={"events": [event]},
            headers=agent_headers("agent-one"),
        )
        assert response.status_code == 422
        assert client.get(f"/api/runs/{run_two['run_id']}").json()["status"] == "running"


def test_revoked_manifest_bootstrap_token_stays_revoked(client: TestClient) -> None:
    runtime_id = _register(client)
    credentials = client.get("/api/admin/tokens").json()
    bootstrap = next(item for item in credentials if item["agent_id"] == "demo-agent")
    revoked = client.delete(f"/api/admin/tokens/{bootstrap['credential_id']}")
    assert revoked.status_code == 200
    heartbeat = client.post(
        "/api/agent/heartbeat",
        json={
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": "2026-08-24T01:00:00Z",
            "status": "ready",
            "active_runs": 0,
        },
        headers=agent_headers(),
    )
    assert heartbeat.status_code == 401


def test_manifest_bootstrap_credential_id_never_reaches_issued_token_kdf(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A predictable bootstrap UUID cannot be presented through the issued-token syntax."""

    runtime_id = _register(client)
    credentials = client.get("/api/admin/tokens").json()
    bootstrap = next(item for item in credentials if item["agent_id"] == "demo-agent")
    called = False

    def forbidden_hash_check(*_: object) -> bool:
        nonlocal called
        called = True
        raise AssertionError("manifest bootstrap credentials must not enter the issued-token KDF")

    monkeypatch.setattr(
        "kitsune_workspace.security._matches_issued_token_hash", forbidden_hash_check
    )
    response = client.post(
        "/api/agent/heartbeat",
        json={
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": "2026-08-24T01:00:00Z",
            "status": "ready",
            "active_runs": 0,
        },
        headers={"Authorization": f"Bearer kt_agent_{bootstrap['credential_id']}.attacker"},
    )

    assert response.status_code == 401
    assert not called
    with client.app.state.database.session() as session:
        credential = session.get(AgentCredential, bootstrap["credential_id"])
        assert credential is not None
        assert credential.kind == "manifest_bootstrap"
        assert credential.token_hash.startswith("scrypt$")


def test_known_issued_credential_id_uses_only_fast_digest_verification(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong secrets for known issued IDs cannot trigger a password KDF."""

    runtime_id = _register(client)
    issued = client.post(
        "/api/admin/tokens",
        json={"agent_id": "demo-agent", "description": "fast digest regression"},
    )
    assert issued.status_code == 200, issued.text
    credential_id = issued.json()["credential_id"]
    called = False

    def forbidden_scrypt(*_: object, **__: object) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("Agent token verification must not use a password KDF")

    monkeypatch.setattr("kitsune_workspace.security.hashlib.scrypt", forbidden_scrypt)
    response = client.post(
        "/api/agent/heartbeat",
        json={
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": "2026-08-24T01:00:00Z",
            "status": "ready",
            "active_runs": 0,
        },
        headers={"Authorization": f"Bearer kt_agent_{credential_id}.attacker"},
    )

    assert response.status_code == 401
    assert not called
    with client.app.state.database.session() as session:
        credential = session.get(AgentCredential, credential_id)
        assert credential is not None
        assert credential.kind == "issued"
        assert credential.token_hash.startswith("sha256$")


@pytest.mark.parametrize("reference_kind", ["env", "file"])
def test_manifest_bootstrap_rotation_and_unavailable_source_fail_closed(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
) -> None:
    """The current env/file secret, rather than a cached digest, authenticates every request."""

    first_token = "bootstrap-token-a"  # noqa: S105 - synthetic credential
    second_token = "bootstrap-token-b"  # noqa: S105 - synthetic credential
    manifest_path = manifest_factory(token=first_token)
    secret_file = tmp_path / "bootstrap-token"
    if reference_kind == "file":
        secret_file.write_text(first_token, encoding="utf-8")
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "env://KITSUNE_DEMO_AGENT_TOKEN",
                f"file://{secret_file}",
            ),
            encoding="utf-8",
        )

    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        registered = client.post(
            "/api/agent/register",
            json=registration(),
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert registered.status_code == 201, registered.text
        runtime_id = registered.json()["runtime_instance_id"]
        heartbeat = {
            "agent_id": "demo-agent",
            "runtime_instance_id": runtime_id,
            "occurred_at": "2026-08-24T01:00:00Z",
            "status": "ready",
            "active_runs": 0,
        }

        if reference_kind == "env":
            monkeypatch.setenv("KITSUNE_DEMO_AGENT_TOKEN", second_token)
        else:
            secret_file.write_text(second_token, encoding="utf-8")

        stale = client.post(
            "/api/agent/heartbeat",
            json=heartbeat,
            headers={"Authorization": f"Bearer {first_token}"},
        )
        current = client.post(
            "/api/agent/heartbeat",
            json=heartbeat,
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert stale.status_code == 401
        assert current.status_code == 200, current.text

        if reference_kind == "env":
            monkeypatch.delenv("KITSUNE_DEMO_AGENT_TOKEN")
        else:
            secret_file.unlink()
        unavailable = client.post(
            "/api/agent/heartbeat",
            json=heartbeat,
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert unavailable.status_code == 401
        assert unavailable.json()["detail"] == "Agent bootstrap secret is unavailable"


def test_webhook_hmac_idempotency_size_and_audit(
    tmp_path: Path, manifest_factory: ManifestFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITSUNE_WEBHOOK_SECRET", "hook-secret")
    triggers = """    - id: incoming
      type: webhook
      handler: default
      hmac_secret_ref: env://KITSUNE_WEBHOOK_SECRET
      max_request_bytes: 128
      rate_limit_per_minute: 10
"""
    manifest_factory(triggers=triggers)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        body = b'{"alert":"disk"}'
        signature = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Kitsune-Signature": f"sha256={signature}",
            "Idempotency-Key": "alert-1",
        }
        first = client.post("/hooks/demo-agent/incoming", content=body, headers=headers)
        second = client.post("/hooks/demo-agent/incoming", content=body, headers=headers)
        assert first.status_code == 202, first.text
        assert second.status_code == 202
        assert first.json()["run_id"] == second.json()["run_id"]
        invalid = client.post(
            "/hooks/demo-agent/incoming",
            content=body,
            headers={"X-Kitsune-Signature": "sha256=bad"},
        )
        assert invalid.status_code == 401
        too_large = b"x" * 129
        large_signature = hmac.new(b"hook-secret", too_large, hashlib.sha256).hexdigest()
        oversized = client.post(
            "/hooks/demo-agent/incoming",
            content=too_large,
            headers={"X-Kitsune-Signature": large_signature},
        )
        assert oversized.status_code == 413
        audit_records = client.get("/api/audit").json()
        assert any(item["action"] == "webhook.dispatch" for item in audit_records)


@pytest.mark.parametrize("reference_kind", ["env", "file"])
def test_webhook_rejects_empty_environment_and_file_hmac_secrets(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
) -> None:
    """An empty resolved HMAC key cannot authenticate a webhook request."""

    if reference_kind == "env":
        monkeypatch.setenv("KITSUNE_EMPTY_WEBHOOK_SECRET", " \t")
        reference = "env://KITSUNE_EMPTY_WEBHOOK_SECRET"
    else:
        secret_file = tmp_path / "empty-webhook-secret"
        secret_file.write_bytes(b"")
        reference = f"file://{secret_file}"
    triggers = f"""    - id: incoming
      type: webhook
      handler: default
      hmac_secret_ref: {reference}
"""
    manifest_factory(triggers=triggers)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        response = client.post(
            "/hooks/demo-agent/incoming",
            content=b'{{"alert":"disk"}}',
            headers={"X-Kitsune-Signature": "sha256=invalid"},
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "webhook secret unavailable"
        assert client.get("/api/runs").json() == []


def test_webhook_shared_secret_and_per_trigger_rate_limit(
    tmp_path: Path, manifest_factory: ManifestFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITSUNE_SHARED_WEBHOOK_SECRET", "shared-hook-secret")
    triggers = """    - id: shared
      type: webhook
      handler: default
      shared_secret_ref: env://KITSUNE_SHARED_WEBHOOK_SECRET
      rate_limit_per_minute: 1
"""
    manifest_factory(triggers=triggers)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        first = client.post(
            "/hooks/demo-agent/shared",
            json={"sequence": 1},
            headers={
                "Authorization": "Bearer shared-hook-secret",
                "Idempotency-Key": "shared-1",
            },
        )
        assert first.status_code == 202, first.text
        limited = client.post(
            "/hooks/demo-agent/shared",
            json={"sequence": 2},
            headers={
                "X-Kitsune-Webhook-Secret": "shared-hook-secret",
                "Idempotency-Key": "shared-2",
            },
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1


def test_unknown_webhook_is_pre_auth_rate_limited_by_source(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path, security={"webhook_rate_limit": 1})
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        first = client.post("/hooks/unknown-agent/unknown-trigger", content=b"x")
        limited = client.post("/hooks/unknown-agent/unknown-trigger", content=b"x")

        assert first.status_code == 404
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1


def test_webhook_authorization_idempotency_is_hashed_and_still_deduplicates(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-idempotency-sentinel"  # noqa: S105 - synthetic leak canary
    monkeypatch.setenv("KITSUNE_WEBHOOK_IDEMPOTENCY_SECRET", secret)
    triggers = """    - id: auth-idempotency
      type: webhook
      handler: default
      shared_secret_ref: env://KITSUNE_WEBHOOK_IDEMPOTENCY_SECRET
      idempotency_header: Authorization
      rate_limit_per_minute: 10
"""
    manifest_factory(triggers=triggers)
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        headers = {"Authorization": f"Bearer {secret}"}
        first = client.post(
            "/hooks/demo-agent/auth-idempotency",
            json={"echo": secret},
            headers=headers,
        )
        duplicate = client.post(
            "/hooks/demo-agent/auth-idempotency",
            json={"echo": "different"},
            headers=headers,
        )

        assert first.status_code == 202, first.text
        assert duplicate.status_code == 202, duplicate.text
        assert duplicate.json()["run_id"] == first.json()["run_id"]
        run_id = first.json()["run_id"]
        with client.app.state.database.session() as session:
            from kitsune_workspace.models import Run

            stored = session.get(Run, run_id)
            assert stored is not None
            assert stored.idempotency_key is not None
            assert len(stored.idempotency_key) == 64
            persisted = {"input": stored.input, "idempotency_key": stored.idempotency_key}
        serialized = repr({"api": client.get(f"/api/runs/{run_id}").json(), "db": persisted})
        assert secret not in serialized
        assert "[REDACTED]" in serialized
