"""Docker log secret reconstruction after Workspace restart."""

from __future__ import annotations

import json
import uuid

import pytest
from conftest import ManifestFactory, settings_for

from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import AgentDefinition, RuntimeInstance
from kitsune_workspace.runtime import (
    RuntimeOperationError,
    _runtime_redacted_environment_names,
    _runtime_secret_values,
    _runtime_snapshot_hash,
)
from kitsune_workspace.util import utcnow


def test_runtime_secret_matching_uses_shared_normalized_key_policy() -> None:
    """Managed injection and recovered log masking recognize the same key variants."""

    snapshot = {
        "spec": {
            "runtime": {
                "secrets": {"EXPLICIT_VALUE": "env://EXPLICIT_VALUE"},
                "environment": {
                    "access_token": "env://ACCESS_TOKEN",
                    "refreshToken": "env://REFRESH_TOKEN",
                    "PRIVATE-KEY": "env://PRIVATE_KEY",
                    "X-API-Key": "env://API_KEY",
                    "X-Tenant-Session": "env://TENANT_SESSION",
                    "safe_monkey": "visible",
                },
            }
        }
    }
    environment = {
        "EXPLICIT_VALUE": "explicit-secret",
        "access_token": "snake-secret",
        "refreshToken": "camel-secret",
        "PRIVATE-KEY": "private-secret",
        "X-API-Key": "header-secret",
        "X-Tenant-Session": "configured-secret",
        "safe_monkey": "visible",
    }
    keys = {"tenantSession"}

    assert _runtime_redacted_environment_names(snapshot, keys) == {
        "EXPLICIT_VALUE",
        "access_token",
        "refreshToken",
        "PRIVATE-KEY",
        "X-API-Key",
        "X-Tenant-Session",
    }
    assert _runtime_secret_values(snapshot, environment, keys) == {
        "explicit-secret",
        "snake-secret",
        "camel-secret",
        "private-secret",
        "header-secret",
        "configured-secret",
    }


@pytest.mark.asyncio
async def test_docker_logs_recover_exact_container_secrets_in_memory_only(
    tmp_path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = manifest_factory(adapter="docker")
    monkeypatch.setenv("KITSUNE_CUSTOM_CONTAINER_SECRET", "new-value-after-rotation")
    monkeypatch.setenv("KITSUNE_PROVIDER_API_KEY", "new-provider-value")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace(
            "    environment: {}",
            "    environment:\n      PROVIDER_API_KEY: env://KITSUNE_PROVIDER_API_KEY",
        )
        .replace(
            "    secrets: {}",
            "    secrets:\n      CUSTOM_CREDENTIAL: env://KITSUNE_CUSTOM_CONTAINER_SECRET",
        ),
        encoding="utf-8",
    )
    settings = settings_for(
        tmp_path,
        security={
            "allow_insecure_external_agents": False,
            "redacted_keys": ["tenantSession"],
        },
    )
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    with database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        environment_contract = control.runtime._runtime_environment_contract(
            snapshot=definition.snapshot,
            adapter="docker",
            agent_id="demo-agent",
            instance_id=str(uuid.uuid4()),
            run=None,
        )
    assert json.loads(environment_contract["KITSUNE_REDACTED_ENVIRONMENT_VARIABLES"]) == [
        "CUSTOM_CREDENTIAL",
        "PROVIDER_API_KEY",
    ]
    old_secret = "old-container-secret"  # noqa: S105 - synthetic leak canary
    container_id = "container-after-restart"
    runtime_id = str(uuid.uuid4())
    with database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        runtime_hash = _runtime_snapshot_hash(definition.snapshot)
        instance = RuntimeInstance(
            runtime_instance_id=runtime_id,
            agent_id="demo-agent",
            adapter="docker",
            mode="resident",
            status="ready",
            container_id=container_id,
            started_at=utcnow(),
            runtime_metadata={"runtime_hash": runtime_hash},
        )
        session.add(instance)

    inspect_calls = 0

    async def inspect(_: str):
        nonlocal inspect_calls
        inspect_calls += 1
        return {
            "Config": {
                "Env": [
                    f"CUSTOM_CREDENTIAL={old_secret}",
                    "ORDINARY=value",
                ]
            }
        }

    async def stream_request(*args, **kwargs):
        del args, kwargs
        return (
            f"credential={old_secret}\n"
            "Authorization: Bearer recovered-bearer\n"
            "api_key=recovered-snake api.key=recovered-dotted "
            "tenant.session=recovered-custom\n"
            "ordinary output\n"
        ).encode()

    monkeypatch.setattr(control.runtime.docker, "inspect", inspect)
    monkeypatch.setattr(control.runtime.docker, "_stream_request", stream_request)

    try:
        with database.session() as session:
            persisted = session.get(RuntimeInstance, runtime_id)
            assert persisted is not None
            source, first = await control.runtime.logs(persisted, 20)
            _, second = await control.runtime.logs(persisted, 20)
        assert source == "docker"
        assert (
            first
            == second
            == [
                "credential=[REDACTED]",
                "Authorization: [REDACTED]",
                "api_key=[REDACTED] api.key=[REDACTED] tenant.session=[REDACTED]",
                "ordinary output",
            ]
        )
        for dynamic_secret in (
            "recovered-bearer",
            "recovered-snake",
            "recovered-dotted",
            "recovered-custom",
        ):
            assert dynamic_secret not in repr(first)
        assert inspect_calls == 1
        with database.session() as session:
            persisted = session.get(RuntimeInstance, runtime_id)
            assert persisted is not None
            assert old_secret not in json.dumps(persisted.runtime_metadata)
            assert "new-value-after-rotation" not in json.dumps(persisted.runtime_metadata)

        control.runtime.docker._redactions.clear()
        with database.session() as session:
            persisted = session.get(RuntimeInstance, runtime_id)
            assert persisted is not None
            persisted.runtime_metadata = {"runtime_hash": "mismatch"}
        with database.session() as session:
            persisted = session.get(RuntimeInstance, runtime_id)
            assert persisted is not None
            with pytest.raises(RuntimeOperationError, match="requires Runtime reconciliation"):
                await control.runtime.logs(persisted, 20)
        assert inspect_calls == 1
    finally:
        await control.runtime.shutdown()
        control.telemetry.shutdown()
        database.dispose()
