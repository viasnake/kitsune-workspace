"""Operator-visible Docker Compose completion path."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("KITSUNE_E2E_URL", "http://127.0.0.1:8080").rstrip("/")
REPOSITORY = Path(__file__).parents[2]
COMPOSE_FILE = REPOSITORY / "deploy" / "docker-compose.yml"


def _wait_for[ResultT](
    description: str,
    operation: Callable[[], ResultT | None],
    *,
    timeout: float = 120,
) -> ResultT:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result is not None:
                return result
        except (httpx.HTTPError, OSError, ValueError) as exc:
            last_error = exc
        time.sleep(1)
    detail = f": {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for {description}{detail}")


def _json(method: str, path: str, body: Any | None = None) -> Any:
    response = httpx.request(method, f"{BASE_URL}{path}", json=body, timeout=10)
    response.raise_for_status()
    return response.json()


def _agent_ready(agent_id: str) -> dict[str, Any] | None:
    agents = _json("GET", "/api/agents")
    return next(
        (
            agent
            for agent in agents
            if agent["agent_id"] == agent_id and agent["actual_state"] == "ready"
        ),
        None,
    )


def _terminal_run(run_id: str) -> dict[str, Any] | None:
    run = _json("GET", f"/api/runs/{run_id}")
    return run if run["status"] in {"succeeded", "failed", "cancelled", "timed_out"} else None


def _successful_run(run_id: str) -> dict[str, Any] | None:
    run = _terminal_run(run_id)
    if run is None:
        return None
    assert run["status"] == "succeeded", run
    return run


def _running_run(run_id: str) -> dict[str, Any] | None:
    run = _json("GET", f"/api/runs/{run_id}")
    return run if run["status"] == "running" and run["runtime_instance_id"] else None


def _exited_runtime_container(runtime_instance_id: str) -> dict[str, Any] | None:
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=org.kitsune.runtime-instance-id={runtime_instance_id}",
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )
    container_id = listed.stdout.strip().splitlines()
    if listed.returncode != 0 or len(container_id) != 1:
        return None
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}", container_id[0]],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        return None
    state = json.loads(inspected.stdout)
    return state if state["Running"] is False else None


def _runtime_container_removed(runtime_instance_id: str) -> bool | None:
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=org.kitsune.runtime-instance-id={runtime_instance_id}",
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    return True if not listed.stdout.strip() else None


def _invoke(agent_id: str, handler: str, input_value: Any) -> dict[str, Any]:
    created = _json(
        "POST",
        f"/api/agents/{agent_id}/runs",
        {"handler": handler, "input": input_value},
    )
    return _wait_for(
        f"{agent_id} Run {created['run_id']} to succeed",
        lambda: _successful_run(created["run_id"]),
    )


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *arguments],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )


def test_compose_operates_all_runtime_modes_and_persists_history() -> None:
    """Exercise Web, resident, ephemeral, external, schedule, telemetry, and restart paths."""

    health = _wait_for("Workspace health", lambda: _json("GET", "/api/health"))
    assert health["database"] == "healthy"
    web = httpx.get(f"{BASE_URL}/", timeout=10)
    web.raise_for_status()
    assert "Kitsune" in web.text

    _wait_for("managed resident readiness", lambda: _agent_ready("managed-resident-agent"))
    _wait_for("external Agent registration", lambda: _agent_ready("external-agent"))

    resident = _invoke(
        "managed-resident-agent",
        "investigate",
        {"message": "compose-e2e", "depth": 2},
    )
    assert resident["output"]["summary"] == "Processed: compose-e2e"

    ephemeral = _invoke(
        "scheduled-ephemeral-agent",
        "refresh",
        {"collection": "compose-e2e"},
    )
    assert ephemeral["output"]["collection"] == "compose-e2e"

    interrupted = _json(
        "POST",
        "/api/agents/scheduled-ephemeral-agent/runs",
        {
            "handler": "refresh",
            "input": {
                "collection": "workspace-outage",
                "delay_seconds": 5,
                "record_usage": True,
            },
        },
    )
    interrupted_running = _wait_for(
        "ephemeral Run to enter its Handler",
        lambda: _running_run(interrupted["run_id"]),
    )
    runtime_instance_id = interrupted_running["runtime_instance_id"]
    _compose("kill", "--signal", "SIGKILL", "workspace")
    exited = _wait_for(
        "ephemeral container to retain its undelivered outbox",
        lambda: _exited_runtime_container(runtime_instance_id),
        timeout=45,
    )
    assert exited["ExitCode"] == 75
    _compose("start", "workspace")
    restarted_health = _wait_for(
        "Workspace health after automatic restart", lambda: _json("GET", "/api/health"), timeout=90
    )
    assert restarted_health["database"] == "healthy"
    recovered = _wait_for(
        "interrupted Run events to be recovered",
        lambda: _successful_run(interrupted["run_id"]),
    )
    assert recovered["output"]["collection"] == "workspace-outage"
    recovered_events = _json("GET", f"/api/runs/{interrupted['run_id']}/events")
    recovered_types = {event["type"] for event in recovered_events}
    assert "scheduled.collection.refreshed" in recovered_types
    assert "kitsune.run.succeeded" in recovered_types
    recovered_usage = _json("GET", f"/api/runs/{interrupted['run_id']}/usage")
    assert len(recovered_usage) == 1
    assert {
        "provider": recovered_usage[0]["provider"],
        "model": recovered_usage[0]["model"],
        "request_count": recovered_usage[0]["request_count"],
        "input_tokens": recovered_usage[0]["input_tokens"],
        "output_tokens": recovered_usage[0]["output_tokens"],
        "total_tokens": recovered_usage[0]["total_tokens"],
    } == {
        "provider": "compose",
        "model": "deterministic",
        "request_count": 1,
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    _wait_for(
        "recovered ephemeral container cleanup",
        lambda: _runtime_container_removed(runtime_instance_id),
    )

    external = _invoke(
        "external-agent",
        "receive",
        {"payload": {"source": "compose-e2e"}},
    )
    assert external["output"]["accepted_keys"] == ["source"]

    schedules = _json("GET", "/api/schedules")
    assert any(item["agent_id"] == "scheduled-ephemeral-agent" for item in schedules)

    def scheduled_run() -> dict[str, Any] | None:
        runs = _json("GET", "/api/runs?agent_id=scheduled-ephemeral-agent&limit=100")
        return next(
            (run for run in runs if run["source"] == "schedule" and run["status"] == "succeeded"),
            None,
        )

    _wait_for("a scheduled ephemeral Run", scheduled_run, timeout=90)

    stopped = _json("POST", "/api/agents/managed-resident-agent/stop")
    assert stopped["status"] == "accepted"

    def managed_stopped() -> dict[str, Any] | None:
        agents = _json("GET", "/api/agents")
        return next(
            (
                agent
                for agent in agents
                if agent["agent_id"] == "managed-resident-agent"
                and agent["actual_state"] == "stopped"
            ),
            None,
        )

    _wait_for("managed resident stop", managed_stopped)
    _json("POST", "/api/agents/managed-resident-agent/start")
    _wait_for("managed resident restart", lambda: _agent_ready("managed-resident-agent"))

    def collector_output() -> str:
        return _compose("logs", "--no-color", "otel-collector").stdout

    def trace_visible() -> str | None:
        output = collector_output()
        return output if re.search(r"\bName\s*:\s*kitsune\.workspace\.dispatch\b", output) else None

    def metric_visible() -> str | None:
        output = collector_output()
        return output if re.search(r"\bName\s*:\s*kitsune_runs_total\b", output) else None

    _wait_for("Kitsune trace export", trace_visible, timeout=90)
    _wait_for("Kitsune metric export", metric_visible, timeout=90)

    run_ids = {resident["run_id"], ephemeral["run_id"], external["run_id"]}
    if os.environ.get("KITSUNE_E2E_RESTART") == "1":
        _compose("restart", "workspace")
        _wait_for("Workspace health after restart", lambda: _json("GET", "/api/health"))
        persisted = {item["run_id"] for item in _json("GET", "/api/runs?limit=200")}
        assert run_ids <= persisted
