"""Bounded Docker response, archive, and diagnostic-cache regressions."""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import ManifestFactory, settings_for

from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.models import RuntimeInstance
from kitsune_workspace.runtime import DockerAdapter, DockerEngineError
from kitsune_workspace.util import utcnow


class TrackingStream(httpx.AsyncByteStream):
    """Expose how far the adapter consumed a chunked response."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _client_factory(
    stream: TrackingStream, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="http://docker",
    )


def _tar_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


@pytest.mark.asyncio
async def test_docker_log_stream_stops_after_crossing_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket, log_response_max_bytes=1_024)
    stream = TrackingStream([b"a" * 700, b"b" * 700, b"must-not-be-consumed"])
    monkeypatch.setattr(adapter, "_client", lambda: _client_factory(stream))

    with pytest.raises(DockerEngineError, match="1024-byte limit"):
        await adapter.tail("container-one", 10)

    assert stream.yielded == 2
    assert stream.closed


@pytest.mark.asyncio
async def test_docker_log_rejects_oversize_content_length_before_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket, log_response_max_bytes=1_024)
    stream = TrackingStream([b"must-not-be-consumed"])
    monkeypatch.setattr(
        adapter,
        "_client",
        lambda: _client_factory(stream, {"Content-Length": "1025"}),
    )

    with pytest.raises(DockerEngineError, match="1024-byte limit"):
        await adapter.tail("container-one", 10)

    assert stream.yielded == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_docker_live_and_archived_logs_use_shared_text_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker tails mask dynamic assignments and known values before archival."""

    adapter = DockerAdapter(tmp_path / "docker.sock", redacted_keys={"tenantSession"})
    adapter.install_redactions("container-one", {"known-raw-secret"})

    async def stream_request(*_: Any, **__: Any) -> bytes:
        return (
            b"Authorization: Bearer bearer-secret api_key=snake-secret "
            b"api.key=dotted-secret tenant.session=custom-secret known-raw-secret\n"
        )

    monkeypatch.setattr(adapter, "_stream_request", stream_request)
    live = await adapter.tail("container-one", 10)
    adapter._cache_archived_logs("container-one", live)
    adapter._redactions.pop("container-one")
    archived = await adapter.tail("container-one", 10)

    assert live == archived
    assert live == [
        "Authorization: [REDACTED] api_key=[REDACTED] api.key=[REDACTED] "
        "tenant.session=[REDACTED] [REDACTED]"
    ]
    for secret in (
        "bearer-secret",
        "snake-secret",
        "dotted-secret",
        "custom-secret",
        "known-raw-secret",
    ):
        assert secret not in repr(live)
        assert secret not in repr(archived)


@pytest.mark.parametrize(
    ("adapter_kwargs", "entries", "message"),
    [
        (
            {"outbox_archive_max_members": 3},
            [
                ("outbox/events.sqlite3", b"db"),
                ("outbox/one", b"1"),
                ("outbox/two", b"2"),
                ("outbox/three", b"3"),
            ],
            "member-count limit",
        ),
        (
            {"outbox_member_max_bytes": 4, "outbox_total_max_bytes": 20},
            [("outbox/events.sqlite3", b"large")],
            "per-member limit",
        ),
        (
            {"outbox_member_max_bytes": 5, "outbox_total_max_bytes": 5},
            [("outbox/events.sqlite3", b"db1"), ("outbox/ignored", b"etc")],
            "extracted-size limit",
        ),
        (
            {},
            [("../escape", b"x"), ("outbox/events.sqlite3", b"db")],
            "unsafe path",
        ),
        (
            {},
            [
                ("outbox/events.sqlite3", b"first"),
                ("outbox/events.sqlite3", b"second"),
            ],
            "duplicate member",
        ),
    ],
)
@pytest.mark.asyncio
async def test_outbox_archive_limits_reject_before_replacing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_kwargs: dict[str, int],
    entries: list[tuple[str, bytes]],
    message: str,
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    adapter = DockerAdapter(socket, **adapter_kwargs)
    archive = _tar_bytes(entries)

    async def stream_request(*_: Any, target: Path | None = None, **__: Any) -> bytes:
        assert target is not None
        await asyncio.to_thread(target.write_bytes, archive)
        return b""

    monkeypatch.setattr(adapter, "_stream_request", stream_request)
    destination = tmp_path / "recovered" / "events.sqlite3"
    destination.parent.mkdir(mode=0o700)
    destination.write_bytes(b"existing")

    with pytest.raises(DockerEngineError, match=message):
        await adapter.export_outbox("container-one", destination)

    assert destination.read_bytes() == b"existing"
    assert not list(destination.parent.glob(".kitsune-outbox-*"))


def test_archived_docker_logs_are_bounded_by_bytes_and_container_count(tmp_path: Path) -> None:
    adapter = DockerAdapter(
        tmp_path / "docker.sock",
        archived_logs_max_bytes=10,
        archived_log_containers=2,
    )

    adapter._cache_archived_logs("container-one", ["abc"])
    adapter._cache_archived_logs("container-two", ["def"])
    adapter._cache_archived_logs("container-three", ["x"])
    assert list(adapter.archived_logs) == ["container-two", "container-three"]
    assert adapter._archived_log_bytes == 6

    adapter._cache_archived_logs("container-four", ["12345678"])
    assert list(adapter.archived_logs) == ["container-four"]
    assert adapter._archived_log_bytes == 9


@pytest.mark.asyncio
async def test_oversize_outbox_response_quarantines_runtime_for_retry(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory(adapter="docker")
    settings = settings_for(
        tmp_path,
        security={
            "allow_insecure_external_agents": False,
            "managed_outbox_max_bytes": 1_024,
            "docker_outbox_member_max_bytes": 2_048,
            "docker_outbox_total_max_bytes": 4_096,
            "docker_outbox_archive_max_bytes": 4_096,
        },
    )
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.registry.reload()
    runtime_id = str(uuid.uuid4())
    destination = tmp_path / "runtime-state" / "demo-agent" / runtime_id / "events.sqlite3"
    with database.session() as session:
        instance = RuntimeInstance(
            runtime_instance_id=runtime_id,
            agent_id="demo-agent",
            adapter="docker",
            mode="resident",
            status="stopped",
            container_id="container-one",
            started_at=utcnow(),
            runtime_metadata={"outbox_path": str(destination)},
        )
        session.add(instance)
    stream = TrackingStream([b"a" * 700, b"b" * 3_500, b"must-not-be-consumed"])
    monkeypatch.setattr(control.runtime.docker, "_client", lambda: _client_factory(stream))

    try:
        assert not await control.runtime._recover_runtime_outbox(instance)
        with database.session() as session:
            quarantined = session.get(RuntimeInstance, runtime_id)
            assert quarantined is not None
            assert quarantined.status == "unhealthy"
            assert quarantined.container_id == "container-one"
            assert quarantined.last_error == "Durable outbox recovery failed (DockerEngineError)"
            assert "outbox_recovery_error" in quarantined.runtime_metadata
            assert quarantined.runtime_metadata["outbox_recovery_error"]["error_type"] == (
                "DockerEngineError"
            )
        assert not destination.exists()
        assert stream.yielded == 2
    finally:
        await control.runtime.shutdown()
        control.telemetry.shutdown()
        database.dispose()
