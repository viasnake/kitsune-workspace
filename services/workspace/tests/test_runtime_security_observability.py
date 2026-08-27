"""Runtime Adapter, auth boundary, SSE, static UI, OpenAPI, and telemetry tests."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import stat
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import ManifestFactory, agent_headers, registration, settings_for
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kitsune_workspace.app import RequestBodyLimitMiddleware, create_app
from kitsune_workspace.config import WorkspaceSettings
from kitsune_workspace.control_plane import EventBus, EventStreamLimitExceeded
from kitsune_workspace.database import Database
from kitsune_workspace.models import AgentDefinition, Run, RuntimeInstance
from kitsune_workspace.openapi import export_openapi
from kitsune_workspace.runtime import (
    DockerAdapter,
    ExternalAdapter,
    ProcessAdapter,
    RuntimeOperationError,
    _require_private_runtime_directory,
)
from kitsune_workspace.telemetry import WorkspaceTelemetry


@pytest.mark.asyncio
async def test_request_body_limit_replays_then_delegates_disconnect() -> None:
    received: list[dict[str, Any]] = []
    upstream = iter(
        (
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        )
    )

    async def receive() -> dict[str, Any]:
        return next(upstream)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        received.append(await receive())
        received.append(await receive())

    middleware = RequestBodyLimitMiddleware(app, maximum=1024)
    await middleware(
        {"type": "http", "headers": [], "method": "GET", "path": "/api/stream"},
        receive,
        lambda _: asyncio.sleep(0),
    )

    assert [message["type"] for message in received] == ["http.request", "http.disconnect"]


@pytest.mark.asyncio
async def test_process_adapter_captures_logs_and_forces_shutdown() -> None:
    adapter = ProcessAdapter()
    snapshot = {
        "spec": {
            "runtime": {
                "process": {
                    "command": [
                        "/bin/sh",
                        "-c",
                        "printf 'ready\\n'; printf 'warning\\n' >&2; sleep 30",
                    ]
                },
                "environment": {},
                "secrets": {},
            }
        }
    }
    result = await adapter.start("runtime-one", snapshot)
    assert result["pid"] > 0
    await asyncio.sleep(0.05)
    lines = adapter.tail("runtime-one", 10)
    assert "stdout: ready" in lines
    assert "stderr: warning" in lines
    exit_code = await adapter.stop("runtime-one", grace_seconds=1)
    assert exit_code is not None
    await adapter.close()


@pytest.mark.asyncio
async def test_docker_adapter_uses_contract_network_and_manifest_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket)
    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
        requests.append((method, path, kwargs))
        if path.startswith("/networks/"):
            return httpx.Response(
                200,
                json={
                    "Id": "network-id",
                    "Name": "kitsune-network",
                    "Driver": "bridge",
                    "Scope": "local",
                },
            )
        if path.startswith("/containers/create"):
            return httpx.Response(201, json={"Id": "container-1"})
        return httpx.Response(204)

    monkeypatch.setattr(adapter, "_request", request)
    snapshot = {
        "spec": {
            "runtime": {
                "docker": {
                    "image": "example.invalid/agent:latest",
                    "command": ["python", "-m", "agent"],
                    "network": "kitsune-network",
                    "volumes": ["/safe/source:/data:ro"],
                    "privileged": False,
                    "host_network": False,
                },
                "environment": {},
                "secrets": {},
            }
        }
    }
    runtime_id = "00000000-0000-0000-0000-000000000001"
    result = await adapter.start(runtime_id, "demo-agent", snapshot)
    assert result == {"container_id": "container-1"}
    create_body = next(
        item[2]["json"] for item in requests if item[1].startswith("/containers/create")
    )
    assert create_body["HostConfig"]["NetworkMode"] == "network-id"
    assert create_body["HostConfig"]["Privileged"] is False
    assert create_body["HostConfig"]["CapDrop"] == ["ALL"]
    assert create_body["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
    assert create_body["HostConfig"]["Memory"] == 536_870_912
    assert create_body["HostConfig"]["MemorySwap"] == 536_870_912
    assert create_body["HostConfig"]["NanoCpus"] == 1_000_000_000
    assert create_body["HostConfig"]["PidsLimit"] == 256
    assert create_body["HostConfig"]["Init"] is True
    assert create_body["HostConfig"]["RestartPolicy"] == {
        "Name": "no",
        "MaximumRetryCount": 0,
    }
    assert create_body["HostConfig"]["Binds"] == ["/safe/source:/data:ro"]
    runtime_name = DockerAdapter.runtime_name(runtime_id)
    assert create_body["NetworkingConfig"] == {
        "EndpointsConfig": {"network-id": {"Aliases": [runtime_name]}}
    }


def test_external_https_is_strict_and_managed_loopback_is_separate() -> None:
    adapter = ExternalAdapter(allow_insecure=False)
    with pytest.raises(RuntimeOperationError, match="HTTPS"):
        adapter.validate_url("http://127.0.0.1:8081")
    assert adapter.validate_url("https://agent.example.invalid") == "https://agent.example.invalid"
    assert adapter.validate_managed_control_url("http://127.0.0.1:8081").startswith("http://")
    with pytest.raises(RuntimeOperationError, match="HTTPS"):
        adapter.validate_managed_control_url("http://agent.example.invalid:8081")
    with pytest.raises(RuntimeOperationError, match="must use HTTP"):
        adapter.validate_managed_control_url("https://127.0.0.1:8081")
    assert adapter.validate_managed_container_url("http://agent.internal:8081") == (
        "http://agent.internal:8081"
    )
    with pytest.raises(RuntimeOperationError, match="must use HTTP"):
        adapter.validate_managed_container_url("https://agent.internal:8081")


def test_none_mode_rejects_cross_site_and_rebinding_requests(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> None:
    """Loopback admin authority is not ambient authority for arbitrary browser origins."""

    manifest_factory()
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        valid_cli_style = client.post("/api/admin/reload")
        valid_browser = client.post(
            "/api/admin/reload", headers={"Origin": "http://127.0.0.1:8080"}
        )
        cross_site_form = client.post(
            "/api/admin/reload",
            content=b"",
            headers={
                "Origin": "https://attacker.example",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        cross_site_runtime = client.post(
            "/api/agents/demo-agent/stop",
            headers={"Origin": "https://attacker.example"},
        )
        null_origin = client.post("/api/admin/reload", headers={"Origin": "null"})
        hostile_host = client.get("/healthz", headers={"Host": "attacker.example:8080"})
        rebinding_host = client.get("/healthz", headers={"Host": "workspace.attacker.invalid:8080"})
        loopback_alias = client.get("/healthz", headers={"Host": "localhost:8080"})

    assert valid_cli_style.status_code == 200
    assert valid_browser.status_code == 200
    assert cross_site_form.status_code == 403
    assert cross_site_runtime.status_code == 403
    assert null_origin.status_code == 403
    assert hostile_host.status_code == 400
    assert rebinding_host.status_code == 400
    assert loopback_alias.status_code == 200


@pytest.mark.asyncio
async def test_none_mode_accepts_ipv6_trusted_origin_with_default_host_port(
    tmp_path: Path,
) -> None:
    """IPv6 fallback origin construction brackets the host and honors HTTP's default port."""

    manifest_directory = tmp_path / "agents"
    manifest_directory.mkdir()
    settings = WorkspaceSettings.model_validate(
        {
            "workspace": {
                "bind": "::1:80",
                "database_url": f"sqlite:///{tmp_path / 'workspace.sqlite3'}",
                "agent_manifest_directory": manifest_directory,
                "runtime_state_directory": tmp_path / "runtime-state",
            },
            "auth": {"mode": "none"},
        }
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://[::1]") as client:
        response = await client.get("/openapi.json", headers={"Origin": "http://[::1]"})

    assert response.status_code == 200


def test_api_rate_limit_returns_429_from_boundary_middleware(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> None:
    """Middleware rate-limit exceptions retain HTTP semantics and security headers."""

    manifest_factory()
    settings = settings_for(
        tmp_path,
        security={"allow_insecure_external_agents": False, "api_rate_limit": 1},
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        allowed = client.get("/api/auth/me")
        limited = client.get("/api/auth/me")

    assert allowed.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate limit exceeded"
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-Request-ID"]
    assert limited.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in limited.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_workspace_body_limit_rejects_dishonest_stream_before_routing(
    tmp_path: Path,
) -> None:
    """Missing Content-Length cannot move a large body into FastAPI validation."""

    settings = settings_for(tmp_path)
    settings.events.max_payload_bytes = 1024
    settings.events.max_input_bytes = 1024
    settings.events.max_output_bytes = 1024
    yielded_chunks = 0

    async def dishonest_body() -> AsyncIterator[bytes]:
        nonlocal yielded_chunks
        for chunk in (b"x" * 40_000, b"y" * 40_000, b"must-not-be-consumed"):
            yielded_chunks += 1
            yield chunk

    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        response = await client.post(
            "/api/auth/me",
            content=dishonest_body(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert yielded_chunks == 2


def test_runtime_directories_are_created_stepwise_with_private_modes(
    tmp_path: Path,
) -> None:
    """A permissive process umask cannot expose any newly created runtime component."""

    target = tmp_path / "runtime-root" / "agent" / "instance"
    previous_umask = os.umask(0)
    try:
        _require_private_runtime_directory(target)
    finally:
        os.umask(previous_umask)

    for directory in (target.parents[1], target.parents[0], target):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    unsafe = tmp_path / "unsafe-runtime-root"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    with pytest.raises(RuntimeOperationError, match="group/world-writable"):
        _require_private_runtime_directory(unsafe / "agent")

    symlink = tmp_path / "runtime-link"
    symlink.symlink_to(target.parents[1], target_is_directory=True)
    with pytest.raises(RuntimeOperationError, match="non-symlink"):
        _require_private_runtime_directory(symlink / "new-agent")


@pytest.mark.asyncio
async def test_control_status_requests_never_consume_agent_response_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke, cancel, and readiness close compromised multi-megabyte bodies at headers."""

    streams: list[UnconsumedControlBody] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = UnconsumedControlBody()
        streams.append(stream)
        status_code = 200 if request.url.path.endswith("readyz") else 202
        headers = {"Content-Length": "1"} if len(streams) % 2 == 0 else {}
        return httpx.Response(status_code, headers=headers, stream=stream)

    transport = httpx.MockTransport(respond)
    async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    adapter = ExternalAdapter(allow_insecure=False)
    credential = "control-secret"  # noqa: S105 - synthetic credential

    await adapter.invoke(
        "https://agent.example.invalid",
        {"run_id": "test"},
        token=credential,
    )
    await adapter.cancel(
        "https://agent.example.invalid",
        "run-one",
        token=credential,
    )
    assert await adapter.ready(
        "https://agent.example.invalid",
        adapter="external",
        token=credential,
    )

    assert len(streams) == 3
    assert all(stream.iterated_chunks == 0 for stream in streams)
    assert all(stream.closed for stream in streams)
    assert all(request.headers["Accept-Encoding"] == "identity" for request in requests)


class UnconsumedControlBody(httpx.AsyncByteStream):
    """Represent an eight MiB attacker response while recording accidental reads."""

    def __init__(self) -> None:
        self.iterated_chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(1024):
            self.iterated_chunks += 1
            yield b"x" * 8192

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_docker_tail_decodes_mixed_and_partial_raw_stream_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket)

    def frame(stream: int, payload: bytes, *, declared_size: int | None = None) -> bytes:
        size = len(payload) if declared_size is None else declared_size
        return bytes([stream, 0, 0, 0]) + size.to_bytes(4, "big") + payload

    content = (
        frame(1, b"stdout line\n")
        + frame(2, b"stderr line\n")
        + frame(1, b"partial", declared_size=20)
    )

    async def stream_request(*_: Any, **__: Any) -> bytes:
        return content

    monkeypatch.setattr(adapter, "_stream_request", stream_request)
    assert await adapter.tail("container-one", 10) == [
        "stdout line",
        "stderr line",
        "partial",
    ]


@pytest.mark.asyncio
async def test_docker_outbox_archive_copies_only_sqlite_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket)
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        for name, content in {
            "kitsune-outbox/events.sqlite3": b"database",
            "kitsune-outbox/events.sqlite3-wal": b"wal",
            "kitsune-outbox/ignored.txt": b"ignored",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    async def stream_request(*_: Any, target: Path | None = None, **__: Any) -> bytes:
        assert target is not None
        await asyncio.to_thread(target.write_bytes, archive_bytes.getvalue())
        return b""

    monkeypatch.setattr(adapter, "_stream_request", stream_request)
    destination = tmp_path / "recovered" / "events.sqlite3"
    assert await adapter.export_outbox("container-one", destination)
    assert destination.read_bytes() == b"database"
    assert Path(f"{destination}-wal").read_bytes() == b"wal"  # noqa: ASYNC240
    assert not (destination.parent / "ignored.txt").exists()
    assert not (tmp_path / "escape").exists()


def test_resident_registration_waits_for_reachable_control_api(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory(adapter="process", mode="resident")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        with client.app.state.database.session() as session:
            instance = session.query(RuntimeInstance).one()
            runtime_id = instance.runtime_instance_id
        body = registration(runtime_id=runtime_id)
        body["control_url"] = "http://127.0.0.1:8081"
        response = client.post("/api/agent/register", json=body, headers=agent_headers())
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "starting"

        heartbeat = client.post(
            "/api/agent/heartbeat",
            json={
                "agent_id": "demo-agent",
                "runtime_instance_id": runtime_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                "status": "ready",
                "active_runs": 0,
            },
            headers=agent_headers(),
        )
        assert heartbeat.status_code == 200
        with client.app.state.database.session() as session:
            assert session.get(RuntimeInstance, runtime_id).status == "starting"

        reachable = False

        async def ready(*_: Any, **__: Any) -> bool:
            return reachable

        monkeypatch.setattr(client.app.state.control.runtime.external, "ready", ready)
        client.portal.call(client.app.state.control.runtime.monitor)
        with client.app.state.database.session() as session:
            assert session.get(RuntimeInstance, runtime_id).status == "starting"
        reachable = True
        client.portal.call(client.app.state.control.runtime.monitor)
        with client.app.state.database.session() as session:
            assert session.get(RuntimeInstance, runtime_id).status == "ready"


def test_external_resident_is_not_dispatched_before_readiness_probe(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory(adapter="external", mode="resident")
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1:8080") as client:
        body = registration()
        response = client.post("/api/agent/register", json=body, headers=agent_headers())
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "starting"
        runtime_id = response.json()["runtime_instance_id"]
        created = client.post(
            "/api/agents/demo-agent/runs",
            json={"handler": "default", "input": {"message": "queued"}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]

        invoked = False
        reachable = False

        async def ready(*_: Any, **__: Any) -> bool:
            return reachable

        async def invoke(*_: Any, **__: Any) -> None:
            nonlocal invoked
            invoked = True

        control = client.app.state.control
        monkeypatch.setattr(control.runtime.external, "ready", ready)
        monkeypatch.setattr(control.runtime.external, "invoke", invoke)
        client.portal.call(control.runtime.monitor)
        assert client.portal.call(control.runs.dispatch_available) == 0
        assert not invoked
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"
        with client.app.state.database.session() as session:
            assert session.get(RuntimeInstance, runtime_id).status == "starting"

        reachable = True
        client.portal.call(control.runtime.monitor)
        assert client.portal.call(control.runs.dispatch_available) == 1
        assert invoked
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"


@pytest.mark.asyncio
async def test_external_lifecycle_never_creates_orphan_pending_instance(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    from kitsune_workspace.control_plane import ControlPlane

    control = ControlPlane(database, settings)
    control.registry.reload()
    await control.reconcile()
    try:
        with database.session() as session:
            assert session.query(RuntimeInstance).count() == 0
        with pytest.raises(RuntimeOperationError, match="registration"):
            await control.runtime.start_agent("demo-agent")
        with database.session() as session:
            assert session.query(RuntimeInstance).count() == 0
    finally:
        control.telemetry.shutdown()
        database.dispose()


@pytest.mark.asyncio
async def test_ephemeral_clean_exit_without_event_is_failed(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory(adapter="process", mode="ephemeral")
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    from kitsune_workspace.control_plane import ControlPlane

    control = ControlPlane(database, settings)
    control.registry.reload()
    with database.session() as session:
        definition = session.get(AgentDefinition, "demo-agent")
        assert definition is not None
        snapshot = copy.deepcopy(definition.snapshot)
        snapshot["spec"]["runtime"]["process"]["command"] = ["/usr/bin/true"]
        definition.snapshot = snapshot
    try:
        run, _ = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={},
        )
        await control.runs.dispatch_available()
        for _ in range(20):
            await asyncio.sleep(0.05)
            await control.runtime.monitor()
            with database.session() as session:
                current = session.get(Run, run.run_id)
                if current is not None and current.status == "failed":
                    break
        with database.session() as session:
            stored = session.get(Run, run.run_id)
            assert stored is not None and stored.status == "failed"
            assert stored.error["type"] == "runtime_missing_outcome"
    finally:
        await control.runtime.shutdown()
        control.telemetry.shutdown()
        database.dispose()


def test_oidc_requires_references_loopback_rules_and_csrf(
    tmp_path: Path, manifest_factory: ManifestFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_factory()
    with pytest.raises(ValueError, match="reference"):
        WorkspaceSettings.model_validate(
            {
                "workspace": {"bind": "127.0.0.1:8080"},
                "auth": {
                    "mode": "oidc",
                    "issuer": "https://issuer.example.invalid",
                    "client_id": "workspace",
                    "client_secret": "literal",
                    "redirect_uri": "https://workspace.example.test/api/auth/callback",
                    "session_secret": "literal",
                },
            }
        )
    with pytest.raises(ValueError, match="loopback"):
        WorkspaceSettings.model_validate(
            {"workspace": {"bind": "0.0.0.0:8080"}, "auth": {"mode": "none"}}
        )
    with pytest.raises(ValueError, match="loopback workspace.public_url"):
        WorkspaceSettings.model_validate(
            {
                "workspace": {
                    "bind": "127.0.0.1:8080",
                    "public_url": "https://workspace.example.invalid",
                },
                "auth": {"mode": "none"},
            }
        )
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("OIDC_SESSION_SECRET", "session-secret-session-secret-32bytes")
    auth = {
        "mode": "oidc",
        "issuer": "https://issuer.example.invalid",
        "client_id": "workspace",
        "client_secret": "env://OIDC_CLIENT_SECRET",
        "redirect_uri": "https://workspace.example.test/api/auth/callback",
        "session_secret": "env://OIDC_SESSION_SECRET",
    }
    with TestClient(
        create_app(settings_for(tmp_path, auth=auth)), base_url="https://testserver"
    ) as client:
        csrf = "csrf-value"
        viewer = client.app.state.oidc.encode(
            {"sub": "viewer", "name": "Viewer", "roles": ["viewer"], "csrf": csrf}, 600
        )
        client.cookies.set("kitsune_session", viewer)
        assert client.get("/api/agents").status_code == 200
        denied = client.post(
            "/api/agents/demo-agent/runs",
            json={"handler": "default", "input": {}},
            headers={"X-CSRF-Token": csrf},
        )
        assert denied.status_code == 403
        operator = client.app.state.oidc.encode(
            {"sub": "operator", "name": "Operator", "roles": ["operator"], "csrf": csrf},
            600,
        )
        client.cookies.set("kitsune_session", operator)
        assert (
            client.post(
                "/api/agents/demo-agent/runs",
                json={"handler": "default", "input": {}},
            ).status_code
            == 403
        )
        accepted = client.post(
            "/api/agents/demo-agent/runs",
            json={"handler": "default", "input": {}},
            headers={"X-CSRF-Token": csrf},
        )
        assert accepted.status_code == 202


@pytest.mark.asyncio
async def test_event_bus_sse_envelope_and_slow_consumer_isolation() -> None:
    bus = EventBus(queue_size=1)
    iterator = bus.subscribe()
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    await bus.publish("run", {"run_id": "one"})
    envelope = await pending
    assert envelope["type"] == "run"
    assert envelope["data"] == {"run_id": "one"}
    await iterator.aclose()


@pytest.mark.asyncio
async def test_event_bus_bounds_and_releases_live_sse_connections() -> None:
    bus = EventBus(
        queue_size=1,
        max_subscribers=2,
        max_per_principal=1,
        max_per_ip=1,
    )
    first = await bus.open_subscription("viewer-one", "192.0.2.1")
    with pytest.raises(EventStreamLimitExceeded, match="principal"):
        await bus.open_subscription("viewer-one", "192.0.2.2")
    with pytest.raises(EventStreamLimitExceeded, match="ip"):
        await bus.open_subscription("viewer-two", "192.0.2.1")

    second = await bus.open_subscription("viewer-two", "192.0.2.2")
    with pytest.raises(EventStreamLimitExceeded, match="global"):
        await bus.open_subscription("viewer-three", "192.0.2.3")

    await bus.close_subscription(first)
    replacement = await bus.open_subscription("viewer-three", "192.0.2.3")
    await bus.close_subscription(second)
    await bus.close_subscription(replacement)
    assert not bus._subscribers


@pytest.mark.parametrize(
    ("existing_principal", "existing_remote", "expected_status"),
    [
        ("local-development", "192.0.2.10", 429),
        ("other-viewer", "192.0.2.11", 503),
    ],
)
def test_sse_endpoint_rejects_connection_before_streaming(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    existing_principal: str,
    existing_remote: str,
    expected_status: int,
) -> None:
    manifest_factory()
    settings = settings_for(
        tmp_path,
        security={
            "sse_max_connections": 2 if expected_status == 429 else 1,
            "sse_max_connections_per_principal": 1,
            "sse_max_connections_per_ip": 1,
        },
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        bus = client.app.state.control.events
        subscription = client.portal.call(
            bus.open_subscription,
            existing_principal,
            existing_remote,
        )
        response = client.get("/api/stream")
        assert response.status_code == expected_status
        assert response.headers["Retry-After"] == "15"
        client.portal.call(bus.close_subscription, subscription)


def test_static_spa_and_deterministic_openapi(
    tmp_path: Path, manifest_factory: ManifestFactory
) -> None:
    manifest_factory()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<main>Kitsune</main>", encoding="utf-8")
    with TestClient(
        create_app(settings_for(tmp_path, static_directory=static)),
        base_url="http://127.0.0.1:8080",
    ) as client:
        assert "Kitsune" in client.get("/agents/demo-agent").text
    first = tmp_path / "openapi-first.json"
    second = tmp_path / "openapi-second.json"
    settings = settings_for(tmp_path)
    export_openapi(first, settings)
    export_openapi(second, settings)
    assert first.read_bytes() == second.read_bytes()
    schema = json.loads(first.read_text(encoding="utf-8"))
    assert "/api/agent/events/batch" in schema["paths"]
    assert "/hooks/{agent_id}/{trigger_id}" in schema["paths"]
    assert schema["components"]["securitySchemes"]["AgentBearer"] == {
        "type": "http",
        "description": "Bearer token issued for one Agent.",
        "scheme": "bearer",
        "bearerFormat": "Kitsune Agent token",
    }
    assert schema["components"]["securitySchemes"]["SessionCookie"]["in"] == "cookie"
    assert schema["paths"]["/api/agents"]["get"]["security"] == [
        {"SessionCookie": []},
        {},
    ]
    assert schema["paths"]["/api/agent/events/batch"]["post"]["security"] == [{"AgentBearer": []}]
    assert schema["paths"]["/api/agent/events/batch"]["post"].get("parameters", []) == []
    csrf_parameters = schema["paths"]["/api/agents/{agent_id}/start"]["post"]["parameters"]
    assert {
        parameter["name"]: parameter["required"]
        for parameter in csrf_parameters
        if parameter["in"] == "header"
    } == {"X-CSRF-Token": False}
    assert "auth.mode=oidc" in next(
        parameter["description"]
        for parameter in csrf_parameters
        if parameter["name"] == "X-CSRF-Token"
    )
    assert schema["components"]["schemas"]["ErrorResponse"] == {
        "additionalProperties": False,
        "description": "Common FastAPI error body returned for documented HTTP failures.",
        "properties": {"detail": {"title": "Detail", "type": "string"}},
        "required": ["detail"],
        "title": "ErrorResponse",
        "type": "object",
    }
    assert "409" in schema["paths"]["/api/agents/{agent_id}/start"]["post"]["responses"]
    assert schema["paths"]["/api/agents/{agent_id}"]["get"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]["oneOf"] == [
        {"$ref": "#/components/schemas/HTTPValidationError"},
        {"$ref": "#/components/schemas/ErrorResponse"},
    ]
    assert schema["paths"]["/api/admin/reload"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ManifestReloadErrorResponse"}
    webhook_responses = schema["paths"]["/hooks/{agent_id}/{trigger_id}"]["post"]["responses"]
    assert {"400", "413"}.issubset(webhook_responses)
    assert "503" not in schema["paths"]["/api/auth/logout"]["post"]["responses"]


def test_telemetry_exports_required_span_and_metrics(tmp_path: Path) -> None:
    settings = WorkspaceSettings.model_validate(
        {
            "workspace": {
                "bind": "127.0.0.1:8080",
                "database_url": f"sqlite:///{tmp_path / 'telemetry.sqlite3'}",
            },
            "auth": {"mode": "none"},
        }
    )
    database = Database(settings.workspace.database_url)
    database.create_schema()
    exporter = InMemorySpanExporter()
    telemetry = WorkspaceTelemetry(settings, database)
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    with telemetry.span("kitsune.workspace.dispatch", agent_id="demo-agent"):
        telemetry.runs_total.add(1, {"agent_id": "demo-agent", "source": "on_demand"})
    secret = "workspace-span-sentinel"  # noqa: S105 - synthetic leak canary
    with pytest.raises(RuntimeError, match=secret):
        with telemetry.span("kitsune.workspace.failure", agent_id="demo-agent"):
            raise RuntimeError(f"Authorization: Bearer {secret}")
    telemetry.tracer_provider.force_flush()
    spans = list(exporter.get_finished_spans())
    assert [span.name for span in spans] == [
        "kitsune.workspace.dispatch",
        "kitsune.workspace.failure",
    ]
    serialized = repr(
        [
            {
                "status": span.status.description,
                "events": [dict(event.attributes or {}) for event in span.events],
            }
            for span in spans
        ]
    )
    assert secret not in serialized
    assert list(spans[1].events[0].attributes or {}) == ["exception.type"]
    assert spans[1].events[0].attributes["exception.type"] == "RuntimeError"
    telemetry.shutdown()
    database.dispose()
