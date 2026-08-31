"""Workspace test fixtures grounded in complete shared-contract Manifests."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kitsune_workspace.app import create_app
from kitsune_workspace.config import WorkspaceSettings

ManifestFactory = Callable[..., Path]


@pytest.fixture
def manifest_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManifestFactory:
    directory = tmp_path / "agents"
    directory.mkdir()

    def write(
        agent_id: str = "demo-agent",
        *,
        adapter: str = "external",
        mode: str = "resident",
        desired_state: str = "running",
        max_concurrency: int = 1,
        queue_capacity: int = 2,
        queue_policy: str = "queue",
        triggers: str = "",
        token: str | None = None,
    ) -> Path:
        variable = f"KITSUNE_{agent_id.upper().replace('-', '_')}_TOKEN"
        monkeypatch.setenv(variable, token or f"secret-{agent_id}")
        if adapter == "external":
            runtime = """    external:
      endpoint: https://agent.example.invalid
      heartbeat_timeout_seconds: 30
"""
        elif adapter == "process":
            runtime = """    process:
      command: [\"/usr/bin/sleep\", \"30\"]
      control_url: http://127.0.0.1:8081
"""
        else:
            control_port = "" if mode == "ephemeral" else "      control_port: 8081\n"
            runtime = f"""    docker:
      image: example.invalid/kitsune-agent:latest
{control_port}
      network: test-agent-network
      volumes: []
"""
        trigger_block = (
            triggers
            or """    - id: manual
      type: on_demand
      handler: default
"""
        )
        content = f"""schema: kitsune.agent
revision: 1
metadata:
  id: {agent_id}
  display_name: {agent_id}
  labels:
    test: workspace
spec:
  runtime:
    adapter: {adapter}
    mode: {mode}
    desired_state: {desired_state}
{runtime}    environment: {{}}
    secrets: {{}}
  invocation:
    default_handler: default
    max_concurrency: {max_concurrency}
    queue_capacity: {queue_capacity}
    queue_policy: {queue_policy}
    timeout_seconds: 30
    cancellation_grace_seconds: 1
    store_input: true
    store_output: true
    retention_days: 30
  triggers:
{trigger_block}  observability:
    service_name: {agent_id}
  security:
    agent_token_ref: env://{variable}
"""
        path = directory / f"{agent_id}.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    return write


def settings_for(
    root: Path,
    *,
    auth: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
    static_directory: Path | None = None,
) -> WorkspaceSettings:
    workspace: dict[str, Any] = {
        "bind": "127.0.0.1:8080",
        "database_url": f"sqlite:///{root / 'workspace.sqlite3'}",
        "agent_manifest_directory": root / "agents",
        "runtime_state_directory": root / "runtime-state",
        "public_url": (
            "https://127.0.0.1:8080"
            if auth is not None and auth.get("mode") == "oidc"
            else "http://127.0.0.1:8080"
        ),
    }
    if static_directory:
        workspace["static_directory"] = static_directory
    return WorkspaceSettings.model_validate(
        {
            "workspace": workspace,
            "auth": auth or {"mode": "none"},
            "security": security or {"allow_insecure_external_agents": False},
            "scheduler": {
                "poll_interval_seconds": 60,
                "retention_interval_seconds": 3600,
            },
        }
    )


@pytest.fixture
def client(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> Iterator[TestClient]:
    manifest_factory()
    with TestClient(
        create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080"
    ) as test_client:
        yield test_client


def registration(agent_id: str = "demo-agent", runtime_id: str | None = None) -> dict[str, Any]:
    return {
        "descriptor": {
            "agent_id": agent_id,
            "application_version": "1.2.3",
            "sdk_version": "1.0.0",
            "framework": "generic",
            "handlers": [
                {
                    "name": "default",
                    "description": "test handler",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "default_timeout_seconds": 30,
                }
            ],
            "plugins": [{"name": "test-plugin", "version": "1.0.0"}],
            "started_at": "2026-08-24T00:00:00Z",
        },
        "runtime_instance_id": runtime_id
        or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"runtime.{agent_id}")),
        "control_url": "https://agent.example.invalid",
    }


def agent_headers(agent_id: str = "demo-agent") -> dict[str, str]:
    return {"Authorization": f"Bearer secret-{agent_id}"}
