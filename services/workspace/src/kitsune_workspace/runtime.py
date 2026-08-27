"""Process, Docker Engine, and External Runtime Adapters."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import signal
import stat
import tarfile
import tempfile
import uuid
from collections import OrderedDict, defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx
from kitsune.logging import is_sensitive_key, normalize_redacted_keys, redact_sensitive_text
from kitsune_contracts import render_observability_url
from sqlalchemy import ColumnElement, or_, select

from .config import WorkspaceSettings, resolve_secret_reference
from .database import Database
from .models import AgentDefinition, Run, RuntimeInstance
from .storage import StorageQuotaService, lock_agent_definition
from .util import ensure_aware, nested, utcnow

if TYPE_CHECKING:
    from .telemetry import WorkspaceTelemetry


class RuntimeOperationError(RuntimeError):
    """Raised when a manifest-declared runtime operation cannot complete."""


class DockerEngineError(RuntimeOperationError):
    """Preserve whether Docker proved absence or merely became unobservable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_private_runtime_directory(path: Path) -> None:
    """Create missing runtime-state components privately and reject unsafe ancestors."""

    target = path.absolute()
    missing: list[Path] = []
    cursor = target
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise RuntimeOperationError(
                    f"Runtime state directory has no existing ancestor: {target}"
                ) from None
            cursor = parent

    _validate_private_runtime_directory(cursor, created=False)
    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
        except FileExistsError:
            pass
        except FileNotFoundError as exc:
            raise RuntimeOperationError(
                f"Runtime state directory parent disappeared during creation: {directory.parent}"
            ) from exc
        _validate_private_runtime_directory(directory, created=created)


def _validate_private_runtime_directory(path: Path, *, created: bool) -> None:
    """Validate one runtime-state directory without following its final component."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeOperationError(f"Runtime state directory disappeared: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeOperationError(f"Runtime state path must be a non-symlink directory: {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise RuntimeOperationError(f"Runtime state directory is not owned by this process: {path}")
    if os.name != "posix":
        return
    if created:
        path.chmod(0o700)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeOperationError(f"Runtime state path changed during creation: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o700 != 0o700 or mode & 0o022:
        raise RuntimeOperationError(
            f"Runtime state directory must be owner-accessible and not group/world-writable: {path}"
        )


def _runtime_environment(
    snapshot: dict[str, Any],
    extra: dict[str, str] | None = None,
    *,
    inherit: bool = True,
) -> dict[str, str]:
    runtime = nested(snapshot, "spec", "runtime", default={})
    environment: dict[str, str] = {}
    if inherit:
        environment["PATH"] = os.environ.get("PATH", os.defpath)
        for key in ("LANG", "LC_ALL", "TZ"):
            if value := os.environ.get(key):
                environment[key] = value
    for section in ("environment", "secrets"):
        for key, reference in (runtime.get(section) or {}).items():
            environment[str(key)] = resolve_secret_reference(str(reference))
    environment.update(extra or {})
    return environment


def _runtime_secret_values(
    snapshot: dict[str, Any],
    environment: dict[str, str],
    redacted_keys: set[str],
) -> set[str]:
    runtime = nested(snapshot, "spec", "runtime", default={}) or {}
    explicit_secret_keys = {str(key) for key in (runtime.get("secrets") or {})}
    normalized = normalize_redacted_keys(frozenset(redacted_keys))
    return {
        value
        for key, value in environment.items()
        if key in explicit_secret_keys or is_sensitive_key(key, normalized)
    }


def _runtime_redacted_environment_names(
    snapshot: dict[str, Any],
    redacted_keys: set[str],
) -> set[str]:
    """Return Manifest environment names whose concrete values are security-sensitive."""

    runtime = nested(snapshot, "spec", "runtime", default={}) or {}
    explicit_secret_keys = {
        str(key) for key in (runtime.get("secrets") or {}) if isinstance(key, str)
    }
    normalized = normalize_redacted_keys(frozenset(redacted_keys))
    secret_like_environment_keys = {
        str(key)
        for key in (runtime.get("environment") or {})
        if isinstance(key, str) and is_sensitive_key(key, normalized)
    }
    return explicit_secret_keys | secret_like_environment_keys


def _runtime_snapshot_hash(snapshot: dict[str, Any]) -> str:
    """Hash only fields that affect an owned runtime process/container."""

    relevant = {
        "runtime": nested(snapshot, "spec", "runtime", default={}) or {},
        "security": nested(snapshot, "spec", "security", default={}) or {},
    }
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_bind_mount(volume: str) -> str:
    parts = volume.split(":")
    if len(parts) not in {2, 3}:
        raise RuntimeOperationError(
            "Docker volumes must use absolute-host:absolute-container[:ro|rw]"
        )
    host_path, container_path = parts[:2]
    mode = parts[2] if len(parts) == 3 else None
    if not Path(host_path).is_absolute() or not Path(container_path).is_absolute():
        raise RuntimeOperationError("Docker bind mount host and container paths must be absolute")
    if mode not in {None, "ro", "rw"}:
        raise RuntimeOperationError("Docker bind mount mode must be ro or rw")
    return volume


_DOCKER_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DOCKER_RESERVED_NETWORKS = frozenset({"bridge", "default", "host", "none"})


def _validate_docker_network_name(value: object) -> str:
    """Defensively require a named network instead of another container's namespace."""

    if not isinstance(value, str):
        raise RuntimeOperationError("Docker Runtime requires an explicit isolated network")
    network = value.strip()
    folded = network.casefold()
    if (
        not network
        or len(network) > 128
        or folded in _DOCKER_RESERVED_NETWORKS
        or folded.startswith(("container:", "service:"))
        or ":" in network
        or "/" in network
        or _DOCKER_NETWORK_NAME.fullmatch(network) is None
    ):
        raise RuntimeOperationError("Docker network must name an explicit isolated bridge network")
    return network


def _validate_docker_working_directory(value: object) -> str | None:
    """Validate Docker WorkingDir independently of manifest parsing."""

    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise RuntimeOperationError(
            "Docker working_directory must be a normalized absolute POSIX path"
        )
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise RuntimeOperationError(
            "Docker working_directory must be a normalized absolute POSIX path"
        )
    return value


def _decode_docker_raw_stream(content: bytes) -> str:
    """Decode Docker's non-TTY multiplexed stdout/stderr response framing."""

    offset = 0
    payload = bytearray()
    decoded_frame = False
    while offset < len(content):
        remaining = len(content) - offset
        if remaining < 8:
            if not decoded_frame:
                return content.decode("utf-8", errors="replace")
            break
        header = content[offset : offset + 8]
        if header[0] not in {0, 1, 2} or header[1:4] != b"\0\0\0":
            if not decoded_frame:
                return content.decode("utf-8", errors="replace")
            break
        frame_length = int.from_bytes(header[4:8], byteorder="big")
        frame_start = offset + 8
        frame_end = frame_start + frame_length
        payload.extend(content[frame_start : min(frame_end, len(content))])
        decoded_frame = True
        if frame_end > len(content):
            break
        offset = frame_end
    return payload.decode("utf-8", errors="replace")


def _linux_process_start_time(pid: int) -> str | None:
    """Read Linux's immutable process start tick for PID-reuse-safe recovery."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing_parenthesis = stat.rfind(")")
    fields = stat[closing_parenthesis + 2 :].split()
    return fields[19] if closing_parenthesis >= 0 and len(fields) > 19 else None


def _process_configuration(snapshot: dict[str, Any]) -> dict[str, Any]:
    return nested(snapshot, "spec", "runtime", "process", default={}) or {}


def _docker_configuration(snapshot: dict[str, Any]) -> dict[str, Any]:
    return nested(snapshot, "spec", "runtime", "docker", default={}) or {}


class ProcessAdapter:
    """Run manifest-declared commands without accepting API command overrides."""

    live_log_line_limit = 10_000
    stream_chunk_bytes = 65_536

    def __init__(
        self,
        redacted_keys: set[str] | None = None,
        *,
        log_line_max_bytes: int = 65_536,
        live_logs_max_bytes: int = 2_097_152,
        archived_logs_max_bytes: int = 33_554_432,
        archived_log_instances: int = 100,
    ) -> None:
        if log_line_max_bytes <= 0 or live_logs_max_bytes < log_line_max_bytes:
            raise ValueError("process log line limit must fit within the live log budget")
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.logs: dict[str, deque[str]] = defaultdict(deque)
        self._live_log_sizes: dict[str, deque[int]] = defaultdict(deque)
        self._live_log_bytes: dict[str, int] = defaultdict(int)
        self._reader_tasks: dict[str, list[asyncio.Task[None]]] = defaultdict(list)
        self.archived_logs: OrderedDict[str, list[str]] = OrderedDict()
        self._archived_log_sizes: dict[str, int] = {}
        self._archived_log_bytes = 0
        self._redactions: dict[str, set[str]] = {}
        self.redacted_keys = redacted_keys or set()
        self._normalized_redacted_keys = normalize_redacted_keys(frozenset(self.redacted_keys))
        self.log_line_max_bytes = log_line_max_bytes
        self.live_logs_max_bytes = live_logs_max_bytes
        self.archived_logs_max_bytes = archived_logs_max_bytes
        self.archived_log_limit = archived_log_instances

    async def start(
        self,
        instance_id: str,
        snapshot: dict[str, Any],
        extra_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start one process and begin bounded stdout/stderr capture."""

        process_config = _process_configuration(snapshot)
        command = process_config.get("command")
        if not isinstance(command, list) or not command:
            raise RuntimeOperationError("process command is missing")
        working_directory = process_config.get("working_directory")
        directory_exists = (
            await asyncio.to_thread(Path(working_directory).is_dir) if working_directory else True
        )
        if not directory_exists:
            raise RuntimeOperationError(
                f"process working directory does not exist: {working_directory}"
            )
        environment = _runtime_environment(snapshot, extra_environment)
        self._redactions[instance_id] = _runtime_secret_values(
            snapshot, environment, self.redacted_keys
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=working_directory,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeOperationError(f"process start failed: {exc}") from exc
        self.processes[instance_id] = process
        if process.stdout is not None:
            self._reader_tasks[instance_id].append(
                asyncio.create_task(self._read_stream(instance_id, "stdout", process.stdout))
            )
        if process.stderr is not None:
            self._reader_tasks[instance_id].append(
                asyncio.create_task(self._read_stream(instance_id, "stderr", process.stderr))
            )
        return {
            "pid": process.pid,
            "process_start_time": _linux_process_start_time(process.pid),
        }

    async def _read_stream(
        self,
        instance_id: str,
        stream_name: str,
        stream: asyncio.StreamReader,
    ) -> None:
        pending = bytearray()
        discarding_oversized_line = False
        while chunk := await stream.read(self.stream_chunk_bytes):
            offset = 0
            while offset < len(chunk):
                newline = chunk.find(b"\n", offset)
                end = newline if newline >= 0 else len(chunk)
                segment = chunk[offset:end]
                if not discarding_oversized_line:
                    if len(pending) + len(segment) > self.log_line_max_bytes:
                        pending.clear()
                        discarding_oversized_line = True
                        self._append_log(
                            instance_id,
                            stream_name,
                            "[OVERSIZED LOG LINE OMITTED]",
                        )
                    else:
                        pending.extend(segment)
                if newline < 0:
                    break
                if not discarding_oversized_line:
                    if pending.endswith(b"\r"):
                        pending.pop()
                    self._append_log(
                        instance_id,
                        stream_name,
                        pending.decode("utf-8", errors="replace"),
                    )
                    pending.clear()
                else:
                    discarding_oversized_line = False
                offset = newline + 1
        if pending and not discarding_oversized_line:
            self._append_log(
                instance_id,
                stream_name,
                pending.decode("utf-8", errors="replace").rstrip("\r"),
            )

    def _append_log(self, instance_id: str, stream_name: str, text: str) -> None:
        sanitized = redact_sensitive_text(
            text,
            self._normalized_redacted_keys,
            frozenset(self._redactions.get(instance_id, set())),
        )
        line = f"{stream_name}: {sanitized}"
        size = len(line.encode("utf-8")) + 1
        if size > self.log_line_max_bytes:
            line = f"{stream_name}: [OVERSIZED LOG LINE OMITTED]"
            size = len(line.encode("utf-8")) + 1
        self.logs[instance_id].append(line)
        self._live_log_sizes[instance_id].append(size)
        self._live_log_bytes[instance_id] += size
        while (
            len(self.logs[instance_id]) > self.live_log_line_limit
            or self._live_log_bytes[instance_id] > self.live_logs_max_bytes
        ):
            self.logs[instance_id].popleft()
            self._live_log_bytes[instance_id] -= self._live_log_sizes[instance_id].popleft()

    def _cache_archived_logs(self, instance_id: str, logs: list[str]) -> None:
        previous_size = self._archived_log_sizes.pop(instance_id, 0)
        self._archived_log_bytes -= previous_size
        self.archived_logs.pop(instance_id, None)
        kept_reversed: list[tuple[str, int]] = []
        kept_bytes = 0
        secrets = frozenset(self._redactions.get(instance_id, set()))
        for line in reversed(logs[-self.live_log_line_limit :]):
            line = redact_sensitive_text(line, self._normalized_redacted_keys, secrets)
            size = len(line.encode("utf-8")) + 1
            if kept_bytes + size > self.archived_logs_max_bytes:
                break
            kept_reversed.append((line, size))
            kept_bytes += size
        self.archived_logs[instance_id] = [line for line, _ in reversed(kept_reversed)]
        self._archived_log_sizes[instance_id] = kept_bytes
        self._archived_log_bytes += kept_bytes
        while (
            len(self.archived_logs) > self.archived_log_limit
            or self._archived_log_bytes > self.archived_logs_max_bytes
        ):
            evicted_id, _ = self.archived_logs.popitem(last=False)
            self._archived_log_bytes -= self._archived_log_sizes.pop(evicted_id)

    async def stop(self, instance_id: str, grace_seconds: int, force: bool = False) -> int | None:
        """Forward TERM to the process group, then KILL after the grace period."""

        process = self.processes.get(instance_id)
        if process is None:
            return None
        if process.returncode is not None:
            exit_code = process.returncode
            await self.cleanup(instance_id)
            return exit_code
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return process.returncode
        if not force:
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                await self.cleanup(instance_id)
                return exit_code
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        exit_code = await process.wait()
        await self.cleanup(instance_id)
        return exit_code

    async def stop_persisted(
        self,
        pid: int,
        expected_start_time: str | None,
        grace_seconds: int,
        force: bool = False,
    ) -> None:
        """Stop an orphaned managed process only when its persisted identity still matches."""

        actual_start_time = _linux_process_start_time(pid)
        if actual_start_time is None:
            return
        if expected_start_time is None or actual_start_time != expected_start_time:
            raise RuntimeOperationError(
                "refusing to signal a recovered process without matching PID identity"
            )
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = asyncio.get_running_loop().time() + max(0, grace_seconds)
        while _linux_process_start_time(pid) == expected_start_time:
            if force or asyncio.get_running_loop().time() >= deadline:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                return
            await asyncio.sleep(0.05)

    def status(self, instance_id: str) -> tuple[bool, int | None] | None:
        """Return in-process liveness and exit code when this process owns the child."""

        process = self.processes.get(instance_id)
        if process is None:
            return None
        return process.returncode is None, process.returncode

    def tail(self, instance_id: str, lines: int) -> list[str]:
        """Return the most recent captured lines."""

        if instance_id in self.archived_logs:
            return self.archived_logs[instance_id][-lines:]
        return list(self.logs.get(instance_id, ()))[-lines:]

    async def cleanup(self, instance_id: str) -> None:
        """Archive a bounded tail and release all per-process ownership maps."""

        if (
            instance_id not in self.processes
            and instance_id not in self.logs
            and instance_id not in self._reader_tasks
            and instance_id not in self._redactions
        ):
            return
        tasks = self._reader_tasks.pop(instance_id, [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        archived = list(self.logs.pop(instance_id, ()))
        self._live_log_sizes.pop(instance_id, None)
        self._live_log_bytes.pop(instance_id, None)
        self._cache_archived_logs(instance_id, archived)
        self.processes.pop(instance_id, None)
        self._redactions.pop(instance_id, None)

    async def close(self) -> None:
        """Cancel stream readers after managed children have been stopped."""

        for tasks in self._reader_tasks.values():
            for task in tasks:
                task.cancel()
        await asyncio.gather(
            *(task for tasks in self._reader_tasks.values() for task in tasks),
            return_exceptions=True,
        )
        for instance_id in list(self.processes):
            await self.cleanup(instance_id)


class DockerAdapter:
    """Minimal Docker Engine API client over the configured Unix socket."""

    api_prefix = "/v1.45"
    stream_chunk_bytes = 65_536
    archived_log_line_limit = 2_000

    def __init__(
        self,
        socket_path: Path,
        redacted_keys: set[str] | None = None,
        *,
        outbox_archive_max_bytes: int = 285_212_672,
        outbox_archive_max_members: int = 64,
        outbox_member_max_bytes: int = 134_217_728,
        outbox_total_max_bytes: int = 268_435_456,
        log_response_max_bytes: int = 2_097_152,
        archived_logs_max_bytes: int = 33_554_432,
        archived_log_containers: int = 100,
    ) -> None:
        self.socket_path = socket_path
        self.archived_logs: OrderedDict[str, list[str]] = OrderedDict()
        self._archived_log_sizes: dict[str, int] = {}
        self._archived_log_bytes = 0
        self._redactions: dict[str, set[str]] = {}
        self.redacted_keys = redacted_keys or set()
        self._normalized_redacted_keys = normalize_redacted_keys(frozenset(self.redacted_keys))
        self.outbox_archive_max_bytes = outbox_archive_max_bytes
        self.outbox_archive_max_members = outbox_archive_max_members
        self.outbox_member_max_bytes = outbox_member_max_bytes
        self.outbox_total_max_bytes = outbox_total_max_bytes
        self.log_response_max_bytes = log_response_max_bytes
        self.archived_logs_max_bytes = archived_logs_max_bytes
        self.archived_log_limit = archived_log_containers

    def needs_redaction_recovery(self, container_id: str) -> bool:
        """Return whether a live container lacks this process's in-memory secret set."""

        return container_id not in self._redactions and container_id not in self.archived_logs

    @staticmethod
    def runtime_name(instance_id: str) -> str:
        """Return the deterministic RFC 1123 container name and network alias."""

        try:
            runtime_id = uuid.UUID(instance_id)
        except ValueError as exc:
            raise RuntimeOperationError("Runtime Instance ID must be a UUID") from exc
        return f"kitsune-runtime-{runtime_id.hex}"

    def install_redactions(self, container_id: str, secrets: set[str]) -> None:
        """Install reconstructed container secret values in memory only."""

        self._redactions[container_id] = set(secrets)

    def _client(self) -> httpx.AsyncClient:
        if not self.socket_path.exists():
            raise RuntimeOperationError(f"Docker socket is unavailable: {self.socket_path}")
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path)),
            base_url="http://docker",
            timeout=30,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, f"{self.api_prefix}{path}", **kwargs)
        except DockerEngineError:
            raise
        except RuntimeOperationError as exc:
            raise DockerEngineError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise DockerEngineError(f"Docker Engine request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise DockerEngineError(
                f"Docker Engine {method} {path} returned {response.status_code}: {detail}",
                response.status_code,
            )
        return response

    @staticmethod
    def _declared_content_length(response: httpx.Response) -> int | None:
        raw_length = response.headers.get("content-length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise DockerEngineError("Docker Engine returned an invalid Content-Length") from exc
        if length < 0:
            raise DockerEngineError("Docker Engine returned an invalid Content-Length")
        return length

    async def _stream_request(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        target: Path | None = None,
    ) -> bytes:
        """Stream one successful Docker response into a bounded buffer or file."""

        output: BinaryIO | None = None
        payload = bytearray()
        received = 0
        completed = False
        try:
            if target is not None:
                output = await asyncio.to_thread(target.open, "xb")
                await asyncio.to_thread(target.chmod, 0o600)
            async with self._client() as client:
                async with client.stream(
                    method,
                    f"{self.api_prefix}{path}",
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code >= 400:
                        detail = bytearray()
                        async for chunk in response.aiter_raw(
                            chunk_size=min(self.stream_chunk_bytes, 1_000)
                        ):
                            detail.extend(chunk[: 1_000 - len(detail)])
                            if len(detail) >= 1_000:
                                break
                        message = detail.decode("utf-8", errors="replace")
                        raise DockerEngineError(
                            f"Docker Engine {method} {path} returned "
                            f"{response.status_code}: {message}",
                            response.status_code,
                        )
                    declared_length = self._declared_content_length(response)
                    if declared_length is not None and declared_length > max_bytes:
                        raise DockerEngineError(
                            f"Docker Engine {method} {path} response exceeds "
                            f"the {max_bytes}-byte limit"
                        )
                    async for chunk in response.aiter_raw(
                        chunk_size=min(self.stream_chunk_bytes, max_bytes + 1)
                    ):
                        received += len(chunk)
                        if received > max_bytes:
                            raise DockerEngineError(
                                f"Docker Engine {method} {path} response exceeds "
                                f"the {max_bytes}-byte limit"
                            )
                        if output is None:
                            payload.extend(chunk)
                        else:
                            output.write(chunk)
                    if declared_length is not None and received != declared_length:
                        raise DockerEngineError(
                            f"Docker Engine {method} {path} response length did not match "
                            "Content-Length"
                        )
                    completed = True
        except DockerEngineError:
            raise
        except RuntimeOperationError as exc:
            raise DockerEngineError(str(exc)) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise DockerEngineError(f"Docker Engine request failed: {exc}") from exc
        finally:
            if output is not None:
                with contextlib.suppress(OSError):
                    output.close()
            if target is not None and not completed:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(target.unlink, missing_ok=True)
        return bytes(payload)

    async def start(
        self,
        instance_id: str,
        agent_id: str,
        snapshot: dict[str, Any],
        extra_environment: dict[str, str] | None = None,
        container_created: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Create and start one manifest-defined container."""

        docker = _docker_configuration(snapshot)
        image = docker.get("image")
        if not isinstance(image, str) or not image or len(image) > 255:
            raise RuntimeOperationError("Docker image must be a non-empty bounded string")
        command = docker.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or len(command) > 256
            or not all(isinstance(part, str) and 1 <= len(part) <= 4096 for part in command)
        ):
            raise RuntimeOperationError("Docker command must be a non-empty bounded string list")
        working_directory = _validate_docker_working_directory(docker.get("working_directory"))
        if docker.get("privileged"):
            raise RuntimeOperationError("privileged Docker containers are prohibited")
        if docker.get("host_network"):
            raise RuntimeOperationError("Docker host networking is prohibited")
        network = _validate_docker_network_name(docker.get("network"))
        inspected_network = (
            await self._request("GET", f"/networks/{quote(network, safe='')}")
        ).json()
        network_id = inspected_network.get("Id")
        if (
            not isinstance(network_id, str)
            or not network_id
            or inspected_network.get("Name") != network
            or inspected_network.get("Driver") != "bridge"
            or inspected_network.get("Scope") != "local"
        ):
            raise RuntimeOperationError(
                "Docker network must be an operator-created local bridge network"
            )
        memory_limit = docker.get("memory_limit_bytes", 536_870_912)
        cpu_limit = docker.get("cpu_limit", 1.0)
        pids_limit = docker.get("pids_limit", 256)
        if (
            not isinstance(memory_limit, int)
            or isinstance(memory_limit, bool)
            or not 67_108_864 <= memory_limit <= 68_719_476_736
        ):
            raise RuntimeOperationError(
                "Docker memory_limit_bytes must be between 64 MiB and 64 GiB"
            )
        if (
            not isinstance(cpu_limit, int | float)
            or isinstance(cpu_limit, bool)
            or not math.isfinite(float(cpu_limit))
            or not 0.01 <= float(cpu_limit) <= 64
        ):
            raise RuntimeOperationError("Docker cpu_limit must be finite and between 0.01 and 64")
        if (
            not isinstance(pids_limit, int)
            or isinstance(pids_limit, bool)
            or not 16 <= pids_limit <= 32_768
        ):
            raise RuntimeOperationError("Docker pids_limit must be between 16 and 32768")
        environment = _runtime_environment(snapshot, extra_environment, inherit=False)
        redactions = _runtime_secret_values(snapshot, environment, self.redacted_keys)
        host_config: dict[str, Any] = {
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Memory": memory_limit,
            "MemorySwap": memory_limit,
            "NanoCpus": max(1, int(round(float(cpu_limit) * 1_000_000_000))),
            "PidsLimit": pids_limit,
            "Init": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "NetworkMode": network_id,
        }
        binds: list[str] = []
        for volume in docker.get("volumes", []):
            if not isinstance(volume, str):
                raise RuntimeOperationError("Docker volumes must be bind mount strings")
            validated_volume = _validate_bind_mount(volume)
            host_path = await asyncio.to_thread(Path(volume.split(":")[0]).resolve, strict=False)
            container_path = PurePosixPath(volume.split(":")[1])
            if ".." in container_path.parts:
                raise RuntimeOperationError(
                    "Docker volume container paths cannot contain dot-dot segments"
                )
            if (
                host_path == self.socket_path.resolve(strict=False)
                or host_path in self.socket_path.resolve(strict=False).parents
                or container_path == PurePosixPath("/var/run/docker.sock")
            ):
                raise RuntimeOperationError(
                    "Manifest volumes cannot expose the Docker Engine socket"
                )
            managed_outbox_mount = PurePosixPath("/var/lib/kitsune-outbox")
            if (
                container_path == managed_outbox_mount
                or container_path in managed_outbox_mount.parents
                or managed_outbox_mount in container_path.parents
            ):
                raise RuntimeOperationError(
                    "Manifest volumes cannot overlap the Workspace-managed outbox mount"
                )
            binds.append(validated_volume)
        if binds:
            host_config["Binds"] = binds
        runtime_name = self.runtime_name(instance_id)
        runtime_hash = _runtime_snapshot_hash(snapshot)
        body: dict[str, Any] = {
            "Image": image,
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "Labels": {
                "org.kitsune.agent-id": agent_id,
                "org.kitsune.runtime-instance-id": instance_id,
                "org.kitsune.runtime-snapshot-hash": runtime_hash,
            },
            "HostConfig": host_config,
            "NetworkingConfig": {
                "EndpointsConfig": {
                    network_id: {"Aliases": [runtime_name]},
                }
            },
        }
        if command is not None:
            body["Cmd"] = command
        if working_directory is not None:
            body["WorkingDir"] = working_directory
        container_running = False
        container_was_created = False
        try:
            create = await self._request(
                "POST", f"/containers/create?name={quote(runtime_name)}", json=body
            )
            container_id = create.json().get("Id")
            if not isinstance(container_id, str) or not container_id:
                raise RuntimeOperationError("Docker Engine returned no Container ID")
            container_was_created = True
        except DockerEngineError as exc:
            if exc.status_code != 409:
                raise
            collision = (
                await self._request("GET", f"/containers/{quote(runtime_name, safe='')}/json")
            ).json()
            labels = (collision.get("Config") or {}).get("Labels") or {}
            expected_labels = {
                "org.kitsune.agent-id": agent_id,
                "org.kitsune.runtime-instance-id": instance_id,
                "org.kitsune.runtime-snapshot-hash": runtime_hash,
            }
            if not isinstance(labels, dict) or any(
                labels.get(key) != value for key, value in expected_labels.items()
            ):
                raise RuntimeOperationError(
                    "Docker container name collision does not match this Runtime Instance"
                ) from exc
            container_id = collision.get("Id")
            if not isinstance(container_id, str) or not container_id:
                raise RuntimeOperationError(
                    "Docker collision recovery returned no Container ID"
                ) from exc
            container_running = bool((collision.get("State") or {}).get("Running"))
        try:
            if container_created is not None:
                container_created(container_id)
            if not container_running:
                await self._request("POST", f"/containers/{container_id}/start")
        except BaseException:
            if container_was_created:
                with contextlib.suppress(RuntimeOperationError):
                    await self._request("DELETE", f"/containers/{container_id}?force=true")
            raise
        self._redactions[container_id] = redactions
        return {"container_id": container_id}

    async def stop(self, container_id: str, grace_seconds: int, force: bool = False) -> None:
        """Stop one container and force-kill it when requested."""

        if force:
            await self._request("POST", f"/containers/{container_id}/kill")
        else:
            await self._request("POST", f"/containers/{container_id}/stop?t={grace_seconds}")

    async def inspect(self, container_id: str) -> dict[str, Any]:
        """Return Docker's current container state."""

        return (await self._request("GET", f"/containers/{container_id}/json")).json()

    async def export_outbox(self, container_id: str, destination: Path) -> bool:
        """Copy a stopped container's durable outbox before deleting the container."""

        _require_private_runtime_directory(destination.parent)
        with tempfile.TemporaryDirectory(
            prefix=".kitsune-outbox-", dir=destination.parent
        ) as temporary_directory:
            staging = Path(temporary_directory)
            archive_path = staging / "outbox.tar"
            try:
                await self._stream_request(
                    "GET",
                    f"/containers/{container_id}/archive?path=%2Fvar%2Flib%2Fkitsune-outbox",
                    max_bytes=self.outbox_archive_max_bytes,
                    target=archive_path,
                )
            except DockerEngineError as exc:
                if exc.status_code == 404:
                    return False
                raise
            copied = await asyncio.to_thread(
                self._stage_outbox_archive,
                archive_path,
                staging,
            )
            if not copied:
                return False
            targets = {
                "events.sqlite3": destination,
                "events.sqlite3-wal": Path(f"{destination}-wal"),
                "events.sqlite3-shm": Path(f"{destination}-shm"),
            }
            try:
                for name in ("events.sqlite3-wal", "events.sqlite3-shm"):
                    staged = staging / name
                    target = targets[name]
                    if staged.exists():
                        staged.replace(target)
                    else:
                        target.unlink(missing_ok=True)
                (staging / "events.sqlite3").replace(destination)
            except OSError as exc:
                raise DockerEngineError(
                    f"Docker outbox archive could not be installed: {exc}"
                ) from exc
            return True

    def _stage_outbox_archive(self, archive_path: Path, staging: Path) -> bool:
        """Validate every tar member before installing any recovered SQLite file."""

        allowed = {"events.sqlite3", "events.sqlite3-wal", "events.sqlite3-shm"}
        copied: set[str] = set()
        member_count = 0
        declared_total = 0
        try:
            with tarfile.open(archive_path, mode="r|") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > self.outbox_archive_max_members:
                        raise DockerEngineError(
                            "Docker outbox archive exceeds the configured member-count limit"
                        )
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise DockerEngineError(
                            f"Docker outbox archive contains an unsafe path: {member.name}"
                        )
                    if member.size < 0 or member.size > self.outbox_member_max_bytes:
                        raise DockerEngineError(
                            f"Docker outbox archive member {member.name} exceeds "
                            "the configured per-member limit"
                        )
                    declared_total += member.size
                    if declared_total > self.outbox_total_max_bytes:
                        raise DockerEngineError(
                            "Docker outbox archive exceeds the configured extracted-size limit"
                        )
                    name = member_path.name
                    if name not in allowed:
                        continue
                    if name in copied:
                        raise DockerEngineError(
                            f"Docker outbox archive contains duplicate member: {name}"
                        )
                    if not member.isfile():
                        raise DockerEngineError(
                            f"Docker outbox archive member is not a regular file: {name}"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise DockerEngineError(
                            f"Docker outbox archive member could not be read: {name}"
                        )
                    remaining = member.size
                    target = staging / name
                    with target.open("xb") as output:
                        target.chmod(0o600)
                        while remaining:
                            chunk = source.read(min(self.stream_chunk_bytes, remaining))
                            if not chunk:
                                raise DockerEngineError(
                                    f"Docker outbox archive member is truncated: {name}"
                                )
                            output.write(chunk)
                            remaining -= len(chunk)
                    copied.add(name)
        except DockerEngineError:
            raise
        except (OSError, tarfile.TarError, ValueError, OverflowError) as exc:
            raise DockerEngineError(f"Docker outbox archive is invalid: {exc}") from exc
        return "events.sqlite3" in copied

    async def tail(self, container_id: str, lines: int) -> list[str]:
        """Fetch recent stdout and stderr without persisting raw logs in Workspace."""

        lines = max(0, lines)
        if lines == 0:
            return []
        if container_id in self.archived_logs:
            return self.archived_logs[container_id][-lines:]
        content = await self._stream_request(
            "GET",
            f"/containers/{container_id}/logs?stdout=true&stderr=true&tail={lines}",
            max_bytes=self.log_response_max_bytes,
        )
        secrets = frozenset(self._redactions.get(container_id, set()))
        return [
            redact_sensitive_text(line, self._normalized_redacted_keys, secrets)
            for line in _decode_docker_raw_stream(content).splitlines()[-lines:]
        ]

    def _cache_archived_logs(self, container_id: str, logs: list[str]) -> None:
        """Keep diagnostic tails within both line/container and encoded-byte limits."""

        previous_size = self._archived_log_sizes.pop(container_id, 0)
        self._archived_log_bytes -= previous_size
        self.archived_logs.pop(container_id, None)
        kept_reversed: list[tuple[str, int]] = []
        kept_bytes = 0
        secrets = frozenset(self._redactions.get(container_id, set()))
        for line in reversed(logs[-self.archived_log_line_limit :]):
            line = redact_sensitive_text(line, self._normalized_redacted_keys, secrets)
            line_bytes = len(line.encode("utf-8")) + 1
            if kept_bytes + line_bytes > self.archived_logs_max_bytes:
                break
            kept_reversed.append((line, line_bytes))
            kept_bytes += line_bytes
        self.archived_logs[container_id] = [line for line, _ in reversed(kept_reversed)]
        self._archived_log_sizes[container_id] = kept_bytes
        self._archived_log_bytes += kept_bytes
        while (
            len(self.archived_logs) > self.archived_log_limit
            or self._archived_log_bytes > self.archived_logs_max_bytes
        ):
            evicted_id, _ = self.archived_logs.popitem(last=False)
            self._archived_log_bytes -= self._archived_log_sizes.pop(evicted_id)

    async def archive_and_remove(self, container_id: str, lines: int = 2_000) -> bool:
        """Retain a bounded diagnostic tail, then remove one terminal container."""

        if container_id in self.archived_logs:
            return True
        archived: list[str] = []
        with contextlib.suppress(RuntimeOperationError):
            archived = await self.tail(
                container_id, max(0, min(lines, self.archived_log_line_limit))
            )
        try:
            await self._request("DELETE", f"/containers/{container_id}?force=true")
        except RuntimeOperationError:
            return False
        self._cache_archived_logs(container_id, archived)
        self._redactions.pop(container_id, None)
        return True


class ExternalAdapter:
    """Invoke and observe externally managed Agents without lifecycle ownership."""

    def __init__(self, allow_insecure: bool) -> None:
        self.allow_insecure = allow_insecure

    def validate_url(
        self,
        url: str,
        *,
        allow_loopback: bool = False,
        allow_managed_http: bool = False,
    ) -> str:
        """Enforce HTTPS unless an explicit development setting allows HTTP."""

        parsed = urlparse(url)
        loopback = False
        if parsed.hostname:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
        if (
            parsed.scheme != "https"
            and not self.allow_insecure
            and not allow_managed_http
            and not (allow_loopback and loopback)
        ):
            raise RuntimeOperationError("external Agent endpoints must use HTTPS")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeOperationError("external Agent endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeOperationError(
                "external Agent endpoint cannot contain credentials, query, or fragment"
            )
        return self.canonical_url(url)

    @staticmethod
    def canonical_url(url: str) -> str:
        """Canonicalize an HTTP(S) base URL for registration comparisons."""

        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
        if (
            port is None
            or (parsed.scheme == "https" and port == 443)
            or (parsed.scheme == "http" and port == 80)
        ):
            netloc = host
        else:
            netloc = f"{host}:{port}"
        path = parsed.path.rstrip("/")
        return urlunparse((parsed.scheme.casefold(), netloc, path, "", "", ""))

    def validate_registration_url(self, declared_url: str, supplied_url: str) -> str:
        """Require an external registration to use its manifest-declared Control URL."""

        declared = self.validate_url(declared_url)
        supplied = self.validate_url(supplied_url)
        if not hmac.compare_digest(declared, supplied):
            raise RuntimeOperationError(
                "external registration control_url must match the manifest endpoint"
            )
        return supplied

    def validate_managed_control_url(self, url: str) -> str:
        """Require the directly served process Control API on HTTP loopback."""

        validated = self.validate_url(url, allow_loopback=True)
        parsed = urlparse(validated)
        hostname = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.casefold() == "localhost"
        if parsed.scheme != "http" or not loopback:
            raise RuntimeOperationError("managed process control_url must use HTTP loopback")
        return validated

    def validate_managed_container_url(self, url: str) -> str:
        """Require HTTP on the explicit network of a Workspace-owned container."""

        validated = self.validate_url(url, allow_managed_http=True)
        if urlparse(validated).scheme != "http":
            raise RuntimeOperationError("managed Docker control_url must use HTTP")
        return validated

    @staticmethod
    async def _status_request(
        method: str,
        url: str,
        *,
        request_timeout: float,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> int:
        """Read only Control API response headers and close without buffering its body."""

        headers = {"Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            async with client.stream(
                method,
                url,
                headers=headers,
                json=json_body,
            ) as response:
                return response.status_code

    async def invoke(
        self,
        control_url: str,
        body: dict[str, Any],
        *,
        allow_loopback: bool = False,
        allow_managed_http: bool = False,
        token: str | None = None,
    ) -> None:
        """Submit a Run and require the asynchronous 202 acceptance contract."""

        if allow_loopback:
            base = self.validate_managed_control_url(control_url)
        elif allow_managed_http:
            base = self.validate_managed_container_url(control_url)
        else:
            base = self.validate_url(control_url)
        try:
            response_status = await self._status_request(
                "POST",
                urljoin(f"{base}/", "_kitsune/runs"),
                request_timeout=10,
                token=token,
                json_body=body,
            )
        except httpx.HTTPError as exc:
            raise RuntimeOperationError(f"Agent Control API transport failed: {exc}") from exc
        if response_status != 202:
            raise RuntimeOperationError(
                f"Agent Control API returned {response_status}, expected 202"
            )

    async def cancel(
        self,
        control_url: str,
        run_id: str,
        *,
        allow_loopback: bool = False,
        allow_managed_http: bool = False,
        token: str | None = None,
    ) -> None:
        """Forward cancellation to a resident or external Agent."""

        if allow_loopback:
            base = self.validate_managed_control_url(control_url)
        elif allow_managed_http:
            base = self.validate_managed_container_url(control_url)
        else:
            base = self.validate_url(control_url)
        try:
            response_status = await self._status_request(
                "POST",
                urljoin(f"{base}/", f"_kitsune/runs/{run_id}/cancel"),
                request_timeout=10,
                token=token,
            )
        except httpx.HTTPError as exc:
            raise RuntimeOperationError(f"Agent cancellation transport failed: {exc}") from exc
        if response_status not in {200, 202, 204, 404, 409}:
            raise RuntimeOperationError(f"Agent cancellation returned {response_status}")

    async def ready(
        self,
        control_url: str,
        *,
        adapter: str,
        token: str | None = None,
    ) -> bool:
        """Probe a resident Control API only after its server can accept requests."""

        if adapter == "process":
            base = self.validate_managed_control_url(control_url)
        elif adapter == "docker":
            base = self.validate_managed_container_url(control_url)
        else:
            base = self.validate_url(control_url)
        try:
            response_status = await self._status_request(
                "GET",
                urljoin(f"{base}/", "_kitsune/readyz"),
                request_timeout=2,
                token=token,
            )
        except httpx.HTTPError:
            return False
        return response_status == 200


Publish = Callable[[str, dict[str, Any]], Awaitable[None]]
OutboxRecovery = Callable[[str, Path], int]


class RuntimeManager:
    """Persist lifecycle state and coordinate concrete Runtime Adapters."""

    def __init__(
        self,
        database: Database,
        settings: WorkspaceSettings,
        publish: Publish,
        telemetry: WorkspaceTelemetry,
    ) -> None:
        self.database = database
        self.settings = settings
        self.publish = publish
        self.telemetry = telemetry
        self.storage = StorageQuotaService(settings)
        self.process = ProcessAdapter(
            settings.security.redacted_keys,
            log_line_max_bytes=settings.security.process_log_line_max_bytes,
            live_logs_max_bytes=settings.security.process_live_logs_max_bytes,
            archived_logs_max_bytes=settings.security.process_archived_logs_max_bytes,
            archived_log_instances=settings.security.process_archived_log_instances,
        )
        self.docker = DockerAdapter(
            settings.security.docker_socket,
            settings.security.redacted_keys,
            outbox_archive_max_bytes=settings.security.docker_outbox_archive_max_bytes,
            outbox_archive_max_members=settings.security.docker_outbox_archive_max_members,
            outbox_member_max_bytes=settings.security.docker_outbox_member_max_bytes,
            outbox_total_max_bytes=settings.security.docker_outbox_total_max_bytes,
            log_response_max_bytes=settings.security.docker_log_response_max_bytes,
            archived_logs_max_bytes=settings.security.docker_archived_logs_max_bytes,
            archived_log_containers=settings.security.docker_archived_log_containers,
        )
        self.external = ExternalAdapter(settings.security.allow_insecure_external_agents)
        self._operation_lock = asyncio.Lock()
        self._restart_tasks: set[asyncio.Task[None]] = set()
        self._restart_by_agent: dict[str, asyncio.Task[None]] = {}
        self._outbox_recovery: OutboxRecovery | None = None
        self.workspace_started_at = utcnow()

    def mark_workspace_started(self) -> None:
        """Record when this control plane became the active Workspace instance."""

        self.workspace_started_at = utcnow()

    def set_outbox_recovery(self, recovery: OutboxRecovery) -> None:
        """Install the trusted SDK outbox recovery boundary owned by the control plane."""

        self._outbox_recovery = recovery

    def _outbox_paths(self, agent_id: str, instance_id: str, adapter: str) -> tuple[Path, str]:
        """Create one Workspace-owned durable outbox location for a Runtime Instance."""

        root = self.settings.workspace.runtime_state_directory.absolute()
        directory = root / agent_id / instance_id
        if not directory.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
            raise RuntimeOperationError("Runtime outbox path escaped the state directory")
        _require_private_runtime_directory(root)
        _require_private_runtime_directory(root / agent_id)
        _require_private_runtime_directory(directory)
        host_path = directory / "events.sqlite3"
        runtime_path = (
            "/var/lib/kitsune-outbox/events.sqlite3" if adapter == "docker" else str(host_path)
        )
        return host_path, runtime_path

    def _agent_token(self, snapshot: dict[str, Any]) -> str:
        reference = nested(snapshot, "spec", "security", "agent_token_ref")
        if not isinstance(reference, str):
            raise RuntimeOperationError("Manifest security.agent_token_ref is missing")
        try:
            token = resolve_secret_reference(reference)
            self.telemetry.register_secrets(token)
            return token
        except (OSError, ValueError) as exc:
            raise RuntimeOperationError("Agent token secret is unavailable") from exc

    def _workspace_url(self, adapter: str) -> str:
        if adapter == "docker":
            target = self.settings.workspace.agent_url or self.settings.workspace.public_url
            if target:
                return target
            raise RuntimeOperationError(
                "workspace.agent_url or public_url is required for Workspace-managed Docker Agents"
            )
        if self.settings.workspace.public_url:
            return self.settings.workspace.public_url
        host = self.settings.workspace.bind_host
        if host in {"0.0.0.0", "::"}:  # noqa: S104 - convert wildcard bind to loopback URL
            host = "127.0.0.1"
        return f"http://{host}:{self.settings.workspace.bind_port}"

    @staticmethod
    def _docker_control_url(snapshot: dict[str, Any], instance_id: str) -> str | None:
        """Derive a resident Container endpoint from its immutable Runtime identity."""

        runtime = nested(snapshot, "spec", "runtime", default={}) or {}
        if runtime.get("adapter") != "docker" or runtime.get("mode") != "resident":
            return None
        docker = runtime.get("docker") or {}
        control_port = docker.get("control_port")
        if not isinstance(control_port, int) or not 1024 <= control_port <= 65535:
            raise RuntimeOperationError(
                "resident Docker Runtime requires control_port between 1024 and 65535"
            )
        return f"http://{DockerAdapter.runtime_name(instance_id)}:{control_port}"

    def _runtime_environment_contract(
        self,
        *,
        snapshot: dict[str, Any],
        adapter: str,
        agent_id: str,
        instance_id: str,
        run: Run | None,
    ) -> dict[str, str]:
        extra = {
            "KITSUNE_AGENT_ID": agent_id,
            "KITSUNE_RUNTIME_INSTANCE_ID": instance_id,
            "KITSUNE_WORKSPACE_URL": self._workspace_url(adapter),
            "KITSUNE_AGENT_TOKEN": self._agent_token(snapshot),
            "KITSUNE_OUTBOX_MAX_BYTES": str(self.settings.security.managed_outbox_max_bytes),
            "KITSUNE_REDACTED_ENVIRONMENT_VARIABLES": json.dumps(
                sorted(
                    _runtime_redacted_environment_names(
                        snapshot,
                        self.settings.security.redacted_keys,
                    )
                ),
                separators=(",", ":"),
            ),
        }
        workspace_url = extra["KITSUNE_WORKSPACE_URL"]
        if (
            adapter == "docker"
            and urlparse(workspace_url).scheme == "http"
            and self.settings.security.allow_insecure_agent_network
        ):
            extra["KITSUNE_ALLOW_INSECURE_WORKSPACE"] = "true"
        runtime = nested(snapshot, "spec", "runtime", default={}) or {}
        if runtime.get("mode") == "resident" and adapter in {"process", "docker"}:
            adapter_configuration = runtime.get(adapter) or {}
            if adapter == "process":
                control_url = adapter_configuration.get("control_url")
                if not isinstance(control_url, str):
                    raise RuntimeOperationError(
                        "resident process Runtime requires a typed control_url"
                    )
                validated_control_url = self.external.validate_managed_control_url(control_url)
            else:
                derived_control_url = self._docker_control_url(snapshot, instance_id)
                assert derived_control_url is not None
                validated_control_url = self.external.validate_managed_container_url(
                    derived_control_url
                )
            parsed_control_url = urlparse(validated_control_url)
            default_port = 443 if parsed_control_url.scheme == "https" else 80
            extra.update(
                {
                    "KITSUNE_CONTROL_URL": validated_control_url,
                    "KITSUNE_BIND_HOST": (
                        str(parsed_control_url.hostname) if adapter == "process" else "0.0.0.0"  # noqa: S104 - container listener, not Workspace
                    ),
                    "KITSUNE_BIND_PORT": str(parsed_control_url.port or default_port),
                }
            )
        if run is not None:
            extra.update(
                {
                    "KITSUNE_RUN_ID": run.run_id,
                    "KITSUNE_HANDLER": run.handler,
                    "KITSUNE_RUN_SOURCE": run.source,
                    "KITSUNE_CORRELATION_ID": run.correlation_id,
                }
            )
            if run.parent_run_id:
                extra["KITSUNE_PARENT_RUN_ID"] = run.parent_run_id
            if run.trace_id:
                extra["KITSUNE_TRACE_ID"] = run.trace_id
            if run.deadline:
                extra["KITSUNE_RUN_DEADLINE"] = run.deadline.isoformat()
        return extra

    @staticmethod
    def _operator_urls(
        snapshot: dict[str, Any],
        *,
        agent_id: str,
        runtime_instance_id: str,
        run: Run | None,
    ) -> tuple[str | None, str | None]:
        observability = nested(snapshot, "spec", "observability", default={}) or {}
        return (
            render_observability_url(
                str(observability.get("log_url_template", "")),
                agent_id=agent_id,
                runtime_instance_id=runtime_instance_id or None,
                run_id=run.run_id if run else None,
                correlation_id=run.correlation_id if run else None,
                trace_id=run.trace_id if run else None,
            ),
            render_observability_url(
                str(observability.get("trace_url_template", "")),
                agent_id=agent_id,
                runtime_instance_id=runtime_instance_id or None,
                run_id=run.run_id if run else None,
                correlation_id=run.correlation_id if run else None,
                trace_id=run.trace_id if run else None,
            ),
        )

    async def start_agent(
        self,
        agent_id: str,
        run: Run | None = None,
        restart_attempt: int = 0,
    ) -> RuntimeInstance:
        """Start a resident or one-Run ephemeral instance from its stored Manifest."""

        async with self._operation_lock:
            with self.database.session() as session:
                definition = lock_agent_definition(session, agent_id)
                if definition is None or not definition.active:
                    raise RuntimeOperationError(f"unknown Agent Definition: {agent_id}")
                if definition.runtime_adapter == "external":
                    raise RuntimeOperationError(
                        "External Runtime Instances are created only by Agent registration"
                    )
                if definition.runtime_mode == "resident" and run is None:
                    existing = session.scalar(
                        select(RuntimeInstance)
                        .where(
                            RuntimeInstance.agent_id == agent_id,
                            RuntimeInstance.status.in_(
                                ["pending", "starting", "ready", "unhealthy"]
                            ),
                        )
                        .order_by(RuntimeInstance.started_at.desc())
                    )
                    if existing is not None:
                        return existing
                storage_usage = self.storage.lock_usage(session, agent_id)
                self.storage.ensure_active_runtime_capacity(session, agent_id)
                self.storage.reserve(storage_usage, runtimes=1)
                instance_id = str(uuid.uuid4())
                outbox_host_path, outbox_runtime_path = self._outbox_paths(
                    agent_id, instance_id, definition.runtime_adapter
                )
                runtime = RuntimeInstance(
                    runtime_instance_id=instance_id,
                    agent_id=agent_id,
                    adapter=definition.runtime_adapter,
                    mode=definition.runtime_mode,
                    status="starting",
                    restart_attempts=restart_attempt,
                    started_at=utcnow(),
                    control_url=self._docker_control_url(definition.snapshot, instance_id),
                    runtime_metadata={
                        "ephemeral_run_id": run.run_id if run else None,
                        "manifest_hash": definition.content_hash,
                        "runtime_hash": _runtime_snapshot_hash(definition.snapshot),
                        "outbox_path": str(outbox_host_path),
                    },
                )
                session.add(runtime)
                snapshot = definition.snapshot
                adapter = definition.runtime_adapter
                if run is not None:
                    persistent_run = session.get(Run, run.run_id)
                    if persistent_run is None:
                        raise RuntimeOperationError("Run disappeared before Runtime launch")
                    persistent_run.runtime_instance_id = instance_id

            def persist_container_id(container_id: str) -> None:
                """Commit Docker ownership before the Engine is asked to start it."""

                with self.database.session() as session:
                    current = session.get(RuntimeInstance, instance_id)
                    if current is None:
                        raise RuntimeOperationError(
                            "Runtime Instance disappeared after Docker container creation"
                        )
                    if current.container_id not in {None, container_id}:
                        raise RuntimeOperationError(
                            "Runtime Instance already owns a different Docker container"
                        )
                    current.container_id = container_id

            try:
                extra = self._runtime_environment_contract(
                    snapshot=snapshot,
                    adapter=adapter,
                    agent_id=agent_id,
                    instance_id=instance_id,
                    run=run,
                )
                extra["KITSUNE_OUTBOX_PATH"] = outbox_runtime_path
                log_url, trace_url = self._operator_urls(
                    snapshot,
                    agent_id=agent_id,
                    runtime_instance_id=instance_id,
                    run=run,
                )
                with self.telemetry.span(
                    "kitsune.runtime.start", agent_id=agent_id, adapter=adapter
                ):
                    if adapter == "process":
                        result = await self.process.start(instance_id, snapshot, extra)
                    elif adapter == "docker":
                        result = await self.docker.start(
                            instance_id,
                            agent_id,
                            snapshot,
                            extra,
                            persist_container_id,
                        )
                    elif adapter == "external":
                        external = nested(snapshot, "spec", "runtime", "external", default={}) or {}
                        control_url = (
                            external.get("control_url")
                            or external.get("url")
                            or external.get("endpoint")
                        )
                        result = (
                            {"control_url": self.external.validate_url(control_url)}
                            if control_url
                            else {}
                        )
                    else:
                        raise RuntimeOperationError(f"unsupported runtime adapter: {adapter}")
            except BaseException as exc:
                with self.database.session() as session:
                    failed = session.get(RuntimeInstance, instance_id)
                    if failed is not None:
                        failed.status = "failed"
                        failed.stopped_at = utcnow()
                        failed.last_error = f"Runtime start failed ({type(exc).__name__})"
                await self.publish(
                    "runtime", {"runtime_instance_id": instance_id, "status": "failed"}
                )
                if run is None and not isinstance(exc, asyncio.CancelledError):
                    self._schedule_restart_after_failure(
                        agent_id,
                        snapshot,
                        restart_attempt=restart_attempt,
                    )
                raise
            with self.database.session() as session:
                started = session.get(RuntimeInstance, instance_id)
                if started is None:
                    raise RuntimeOperationError("runtime record disappeared during start")
                pid = result.get("pid")
                container_id = result.get("container_id")
                control_url = result.get("control_url")
                health_url = result.get("health_url")
                started.pid = pid if isinstance(pid, int) else None
                process_start_time = result.get("process_start_time")
                if isinstance(process_start_time, str):
                    metadata = dict(started.runtime_metadata or {})
                    metadata["process_start_time"] = process_start_time
                    started.runtime_metadata = metadata
                if isinstance(container_id, str):
                    started.container_id = container_id
                if isinstance(control_url, str):
                    started.control_url = control_url
                if isinstance(health_url, str):
                    started.health_url = health_url
                started.log_url = log_url
                started.trace_url = trace_url
                started.status = (
                    "ready"
                    if run is not None
                    else ("pending" if adapter == "external" else "starting")
                )
                if started.status == "ready":
                    started.ready_at = utcnow()
                runtime = started
                if run is not None:
                    persistent_run = session.get(Run, run.run_id)
                    if persistent_run is not None:
                        persistent_run.runtime_instance_id = instance_id
                        persistent_run.log_url = log_url
                        persistent_run.trace_url = trace_url
            await self.publish(
                "runtime",
                {
                    "runtime_instance_id": instance_id,
                    "agent_id": agent_id,
                    "status": runtime.status,
                },
            )
            return runtime

    async def stop_instance(
        self,
        instance_id: str,
        force: bool = False,
        terminal_status: str = "stopped",
    ) -> RuntimeInstance:
        """Gracefully stop one owned instance; external instances are only marked lost/stopped."""

        with self.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            if instance is None:
                raise RuntimeOperationError(f"unknown Runtime Instance: {instance_id}")
            if instance.status in {"stopped", "failed", "lost"}:
                return instance
            instance.status = "stopping"
            adapter = instance.adapter
            container_id = instance.container_id
            definition = session.get(AgentDefinition, instance.agent_id)
            invocation = (
                nested(definition.snapshot, "spec", "invocation", default={})
                if definition is not None
                else {}
            )
            grace_seconds = int(
                (invocation or {}).get(
                    "cancellation_grace_seconds",
                    self.settings.scheduler.cancel_grace_seconds,
                )
            )
        await self.publish("runtime", {"runtime_instance_id": instance_id, "status": "stopping"})
        error: str | None = None
        exit_code: int | None = None
        container_removed = False
        outbox_recovered = False
        physical_stop_confirmed = False
        failed_run_ids: list[str] = []
        try:
            with self.telemetry.span("kitsune.runtime.stop", adapter=adapter, force=force):
                if adapter == "process":
                    if self.process.status(instance_id) is None and instance.pid is not None:
                        await self.process.stop_persisted(
                            instance.pid,
                            (instance.runtime_metadata or {}).get("process_start_time"),
                            grace_seconds,
                            force,
                        )
                    else:
                        exit_code = await self.process.stop(instance_id, grace_seconds, force)
                    physical_stop_confirmed = True
                    await self.process.cleanup(instance_id)
                    with self.database.session() as session:
                        stopped = session.get(RuntimeInstance, instance_id)
                    if stopped is not None:
                        outbox_recovered = await self._recover_runtime_outbox(stopped)
                elif adapter == "docker" and container_id:
                    await self.docker.stop(container_id, grace_seconds, force)
                    physical_stop_confirmed = True
                    with self.database.session() as session:
                        stopped = session.get(RuntimeInstance, instance_id)
                    if stopped is not None:
                        outbox_recovered = await self._recover_runtime_outbox(stopped)
                    if outbox_recovered:
                        container_removed = await self.docker.archive_and_remove(container_id)
                else:
                    physical_stop_confirmed = True
                    outbox_recovered = True
        except RuntimeOperationError as exc:
            error = f"Runtime stop failed ({type(exc).__name__})"
            terminal_status = "failed" if physical_stop_confirmed else "stopping"
        if not outbox_recovered and adapter in {"process", "docker"}:
            if physical_stop_confirmed:
                terminal_status = "unhealthy"
                error = error or "durable outbox recovery is pending"
            else:
                terminal_status = "stopping"
        with self.database.session() as session:
            definition = lock_agent_definition(session, instance.agent_id)
            if definition is None:
                raise RuntimeOperationError("Agent Definition disappeared during stop")
            instance = session.get(RuntimeInstance, instance_id)
            if instance is None:
                raise RuntimeOperationError("runtime record disappeared during stop")
            instance.status = terminal_status
            if physical_stop_confirmed:
                instance.stopped_at = utcnow()
            instance.last_exit_code = exit_code
            if error:
                instance.last_error = error
            if instance.mode == "resident" and terminal_status != "unhealthy":
                failed_run_ids = self._fail_assigned_runs(
                    session,
                    instance_id=instance_id,
                    exit_code=exit_code,
                    message="resident runtime stopped before assigned Run completion",
                )
            if container_removed:
                instance.container_removed_at = utcnow()
        await self.publish(
            "runtime", {"runtime_instance_id": instance_id, "status": terminal_status}
        )
        for failed_run_id in failed_run_ids:
            await self.publish("run", {"run_id": failed_run_id, "status": "failed"})
        if outbox_recovered and (adapter == "process" or container_removed):
            self._remove_runtime_outbox(instance_id)
        return instance

    def _fail_assigned_runs(
        self,
        session: Any,
        *,
        instance_id: str,
        exit_code: int | None,
        message: str,
    ) -> list[str]:
        """Fail every still-active Run assigned to one terminal resident Runtime."""

        now = utcnow()
        runs = list(
            session.scalars(
                select(Run).where(
                    Run.runtime_instance_id == instance_id,
                    Run.status.not_in(["succeeded", "failed", "cancelled", "timed_out"]),
                )
            )
        )
        for run in runs:
            run.status = "failed"
            run.ended_at = now
            error = {
                "type": "runtime_exit",
                "message": message,
                "retryable": False,
                "details": {"exit_code": exit_code},
            }
            self.storage.set_internal_run_error(run, error)
            if not run.store_input:
                run.input = None
            if not run.store_output:
                run.output = None
        return [run.run_id for run in runs]

    async def stop_agent(self, agent_id: str, force: bool = False) -> list[RuntimeInstance]:
        """Stop every non-terminal Runtime Instance for an Agent."""

        with self.database.session() as session:
            ids = list(
                session.scalars(
                    select(RuntimeInstance.runtime_instance_id).where(
                        RuntimeInstance.agent_id == agent_id,
                        RuntimeInstance.status.in_(
                            ["pending", "starting", "ready", "unhealthy", "stopping"]
                        ),
                    )
                )
            )
        return [await self.stop_instance(instance_id, force) for instance_id in ids]

    async def restart_agent(self, agent_id: str, *, force_retry: bool = True) -> RuntimeInstance:
        """Stop current resident instances and start a fresh Runtime Instance."""

        pending = self._restart_by_agent.pop(agent_id, None)
        if pending is not None and pending is not asyncio.current_task():
            pending.cancel()
        await self.stop_agent(agent_id)
        return await self.start_agent(agent_id, restart_attempt=0 if force_retry else 1)

    async def dispatch_resident(self, run: Run, instance: RuntimeInstance) -> None:
        """Submit a queued Run to a ready resident or External Agent."""

        if not instance.control_url:
            raise RuntimeOperationError("ready Runtime Instance has no Control API URL")
        from kitsune_contracts import AgentRunAssignment

        assignment = AgentRunAssignment.model_validate(
            {
                "run_id": run.run_id,
                "agent_id": run.agent_id,
                "handler": run.handler,
                "source": run.source,
                "parent_run_id": run.parent_run_id,
                "correlation_id": run.correlation_id,
                "trace_id": run.trace_id,
                "input": run.input,
                "deadline": run.deadline,
            }
        )
        body = assignment.model_dump(mode="json", exclude_none=True)
        with self.database.session() as session:
            definition = session.get(AgentDefinition, run.agent_id)
            if definition is None:
                raise RuntimeOperationError(f"unknown Agent Definition: {run.agent_id}")
            token = self._agent_token(definition.snapshot)
        with self.telemetry.span(
            "kitsune.workspace.dispatch", agent_id=run.agent_id, source=run.source
        ):
            await self.external.invoke(
                instance.control_url,
                body,
                allow_loopback=instance.adapter == "process",
                allow_managed_http=instance.adapter == "docker",
                token=token,
            )

    async def cancel_run(self, run: Run, timed_out: bool = False, force: bool = False) -> None:
        """Forward cancellation, wait for acknowledgement, then apply adapter ownership rules."""

        terminal = "timed_out" if timed_out else "cancelled"
        if run.status in {"created", "queued"}:
            await self._mark_run_terminal(run.run_id, terminal)
            return
        instance: RuntimeInstance | None = None
        definition: AgentDefinition | None = None
        if run.runtime_instance_id:
            with self.database.session() as session:
                instance = session.get(RuntimeInstance, run.runtime_instance_id)
                definition = session.get(AgentDefinition, run.agent_id)
        invocation = (
            nested(definition.snapshot, "spec", "invocation", default={})
            if definition is not None
            else {}
        ) or {}
        grace = float(
            invocation.get(
                "cancellation_grace_seconds", self.settings.scheduler.cancel_grace_seconds
            )
        )
        if instance is not None:
            if instance.mode == "ephemeral" and instance.adapter in {"process", "docker"}:
                changed = await self._mark_run_terminal(run.run_id, terminal)
                if changed:
                    await self.stop_instance(instance.runtime_instance_id, force=force)
                return
            elif instance.control_url:
                token = self._agent_token(definition.snapshot) if definition is not None else None
                try:
                    await self.external.cancel(
                        instance.control_url,
                        run.run_id,
                        allow_loopback=instance.adapter == "process",
                        allow_managed_http=instance.adapter == "docker",
                        token=token,
                    )
                except RuntimeOperationError as exc:
                    with self.database.session() as session:
                        current = session.get(RuntimeInstance, instance.runtime_instance_id)
                        if current is not None and current.status not in {
                            "stopped",
                            "failed",
                            "lost",
                        }:
                            current.last_error = (
                                f"Runtime cancellation request failed ({type(exc).__name__})"
                            )
                if await self._wait_for_terminal(run.run_id, grace):
                    return
                if instance.adapter in {"process", "docker"}:
                    changed = await self._mark_run_terminal(run.run_id, terminal)
                    if changed:
                        await self.stop_instance(instance.runtime_instance_id, force=True)
                    return
                else:
                    if not await self._mark_run_terminal(run.run_id, terminal):
                        return
                    with self.database.session() as session:
                        current = session.get(RuntimeInstance, instance.runtime_instance_id)
                        if current is not None and current.status not in {
                            "stopped",
                            "failed",
                            "lost",
                        }:
                            current.status = "unhealthy"
                            current.last_error = "Agent did not acknowledge Run cancellation"
                            metadata = dict(current.runtime_metadata or {})
                            metadata["cancellation_unconfirmed"] = {
                                "run_id": run.run_id,
                                "at": utcnow().isoformat(),
                            }
                            current.runtime_metadata = metadata
                    await self.publish(
                        "runtime",
                        {
                            "runtime_instance_id": instance.runtime_instance_id,
                            "status": "unhealthy",
                        },
                    )
                    return
        await self._mark_run_terminal(run.run_id, terminal)

    async def _wait_for_terminal(self, run_id: str, grace_seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0, grace_seconds)
        while True:
            with self.database.session() as session:
                run = session.get(Run, run_id)
                if run is None or run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
                    return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.1, remaining))

    async def _mark_run_terminal(self, run_id: str, terminal: str) -> bool:
        changed = False
        with self.database.session() as session:
            persistent = session.get(Run, run_id)
            if persistent is not None and persistent.status not in {
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
            }:
                persistent.status = terminal
                persistent.ended_at = utcnow()
                if not persistent.store_input:
                    persistent.input = None
                changed = True
        if changed:
            await self.publish("run", {"run_id": run_id, "status": terminal})
        return changed

    async def logs(self, instance: RuntimeInstance, lines: int) -> tuple[str, list[str]]:
        """Read recent logs from the owning adapter without database duplication."""

        if instance.adapter == "process":
            return "process", self.process.tail(instance.runtime_instance_id, lines)
        if instance.adapter == "docker" and instance.container_id:
            if self.docker.needs_redaction_recovery(instance.container_id):
                await self._recover_docker_log_redactions(instance)
            return "docker", await self.docker.tail(instance.container_id, lines)
        return "external", []

    async def _recover_docker_log_redactions(self, instance: RuntimeInstance) -> None:
        """Rebuild redactions from the exact persisted snapshot and container environment."""

        if not instance.container_id:
            raise RuntimeOperationError("Docker Runtime Instance has no container identity")
        with self.database.session() as session:
            definition = session.get(AgentDefinition, instance.agent_id)
            snapshot = definition.snapshot if definition is not None else None
        expected_hash = (instance.runtime_metadata or {}).get("runtime_hash")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(expected_hash, str)
            or not hmac.compare_digest(expected_hash, _runtime_snapshot_hash(snapshot))
        ):
            raise RuntimeOperationError(
                "Docker log redaction state requires Runtime reconciliation"
            )
        inspected = await self.docker.inspect(instance.container_id)
        configuration = inspected.get("Config")
        raw_environment = configuration.get("Env") if isinstance(configuration, dict) else None
        if not isinstance(raw_environment, list):
            raise RuntimeOperationError("Docker inspect omitted the container environment")
        environment: dict[str, str] = {}
        for entry in raw_environment:
            if not isinstance(entry, str) or "=" not in entry:
                raise RuntimeOperationError("Docker inspect returned a malformed environment")
            key, value = entry.split("=", 1)
            if not key:
                raise RuntimeOperationError("Docker inspect returned a malformed environment")
            environment[key] = value
        secrets = _runtime_secret_values(
            snapshot,
            environment,
            self.settings.security.redacted_keys,
        )
        self.telemetry.register_secrets(*secrets)
        self.docker.install_redactions(instance.container_id, secrets)

    async def _recover_runtime_outbox(self, instance: RuntimeInstance) -> bool:
        """Recover a stopped managed Runtime outbox before any destructive cleanup."""

        raw_path = (instance.runtime_metadata or {}).get("outbox_path")
        if not isinstance(raw_path, str) or self._outbox_recovery is None:
            return True
        path = Path(raw_path)
        try:
            if instance.adapter == "docker" and instance.container_id:
                await self.docker.export_outbox(instance.container_id, path)
            recovered = await asyncio.to_thread(self._outbox_recovery, instance.agent_id, path)
        except Exception as exc:
            with self.database.session() as session:
                current = session.get(RuntimeInstance, instance.runtime_instance_id)
                if current is not None:
                    current.status = "unhealthy"
                    current.last_error = f"Durable outbox recovery failed ({type(exc).__name__})"
                    metadata = dict(current.runtime_metadata or {})
                    metadata["outbox_recovery_error"] = {
                        "at": utcnow().isoformat(),
                        "error_type": type(exc).__name__,
                    }
                    current.runtime_metadata = metadata
            await self.publish(
                "runtime",
                {
                    "runtime_instance_id": instance.runtime_instance_id,
                    "status": "unhealthy",
                },
            )
            return False
        with self.database.session() as session:
            current = session.get(RuntimeInstance, instance.runtime_instance_id)
            if current is not None:
                metadata = dict(current.runtime_metadata or {})
                metadata.pop("outbox_recovery_error", None)
                metadata["outbox_recovered_at"] = utcnow().isoformat()
                metadata["outbox_recovered_events"] = recovered
                current.runtime_metadata = metadata
                if current.last_error and current.last_error.startswith(
                    "Durable outbox recovery failed ("
                ):
                    current.last_error = None
        return True

    def _remove_runtime_outbox(self, instance_id: str) -> None:
        """Remove only a recovered outbox beneath the configured Runtime state root."""

        with self.database.session() as session:
            instance = session.get(RuntimeInstance, instance_id)
            raw_path = (instance.runtime_metadata or {}).get("outbox_path") if instance else None
            recovered = bool(
                instance and (instance.runtime_metadata or {}).get("outbox_recovered_at")
            )
        if not isinstance(raw_path, str) or not recovered:
            return
        root = self.settings.workspace.runtime_state_directory.resolve()
        path = Path(raw_path).resolve()
        if not path.is_relative_to(root):
            raise RuntimeOperationError("Refusing to remove an outbox outside Runtime state")
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()

    async def monitor(self, *, respect_backoff: bool = False) -> None:
        """Observe one bounded, concurrent batch within an aggregate cycle deadline."""

        now = utcnow()
        with self.database.session() as session:
            conditions: list[ColumnElement[bool]] = [
                RuntimeInstance.status.in_(
                    ["pending", "starting", "ready", "unhealthy", "stopping"]
                )
            ]
            if respect_backoff:
                conditions.append(
                    or_(
                        RuntimeInstance.next_probe_at.is_(None),
                        RuntimeInstance.next_probe_at <= now,
                    )
                )
            instances = list(
                session.scalars(
                    select(RuntimeInstance)
                    .where(*conditions)
                    .order_by(
                        RuntimeInstance.next_probe_at.asc().nulls_first(),
                        RuntimeInstance.runtime_instance_id,
                    )
                    .limit(self.settings.scheduler.runtime_probe_batch_size)
                )
            )
        semaphore = asyncio.Semaphore(self.settings.scheduler.runtime_probe_concurrency)

        async def observe(instance: RuntimeInstance) -> None:
            async with semaphore:
                await self._monitor_instance(instance, now)

        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(observe(instance)) for instance in instances
        ]
        tasks.append(asyncio.create_task(self.cleanup_terminal_containers()))
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.settings.scheduler.runtime_probe_cycle_timeout_seconds,
            )
        except BaseException:
            # asyncio.wait() does not cancel its children when this monitor is
            # cancelled.  Drain them here so shutdown cannot leave an external
            # probe or container cleanup task running after the monitor exits.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        results = await asyncio.gather(*done, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _monitor_instance(self, instance: RuntimeInstance, now: Any) -> None:
        """Observe one Runtime Instance and persist a bounded retry schedule."""

        if instance.status == "stopping":
            with contextlib.suppress(RuntimeOperationError):
                await self.stop_instance(instance.runtime_instance_id)
            return
        startup_reference = instance.started_at
        registered_at = (instance.runtime_metadata or {}).get("registered_at")
        if isinstance(registered_at, str):
            with contextlib.suppress(ValueError):
                startup_reference = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
        if (
            instance.status in {"pending", "starting"}
            and startup_reference is not None
            and (now - ensure_aware(startup_reference)).total_seconds()
            > self.settings.scheduler.runtime_startup_timeout_seconds
        ):
            stopped = await self.stop_instance(
                instance.runtime_instance_id,
                force=True,
                terminal_status="failed",
            )
            restart_snapshot: dict[str, Any] | None = None
            with self.database.session() as session:
                definition = lock_agent_definition(session, instance.agent_id)
                current = session.get(RuntimeInstance, instance.runtime_instance_id)
                if current is not None and current.status == "failed":
                    current.last_error = "Runtime startup deadline exceeded"
                    if definition is not None and current.adapter in {"process", "docker"}:
                        restart_snapshot = definition.snapshot
            if restart_snapshot is not None and stopped.mode == "resident":
                self._schedule_restart_after_failure(
                    instance.agent_id,
                    restart_snapshot,
                    restart_attempt=instance.restart_attempts,
                )
            return
        exited = False
        observation_succeeded = True
        exit_code: int | None = None
        if instance.adapter == "process":
            process_status = self.process.status(instance.runtime_instance_id)
            if process_status is None and instance.pid:
                try:
                    os.kill(instance.pid, 0)
                except OSError:
                    exited = True
            elif process_status is not None:
                alive, exit_code = process_status
                exited = not alive
        elif instance.adapter == "docker" and instance.container_id:
            try:
                state = (await self.docker.inspect(instance.container_id))["State"]
                exited = not bool(state.get("Running"))
                exit_code = state.get("ExitCode") if exited else None
                with self.database.session() as session:
                    definition = lock_agent_definition(session, instance.agent_id)
                    if definition is None:
                        return
                    current = session.get(RuntimeInstance, instance.runtime_instance_id)
                    if current is not None and current.status not in {
                        "stopped",
                        "failed",
                        "lost",
                    }:
                        metadata = dict(current.runtime_metadata or {})
                        if metadata.pop("docker_observation_unavailable", None) is not None:
                            current.runtime_metadata = metadata
                            current.last_error = None
                            if current.status == "unhealthy":
                                current.status = "ready"
            except DockerEngineError as exc:
                if exc.status_code == 404:
                    exited = True
                else:
                    observation_succeeded = False
                    publish_unhealthy = False
                    with self.database.session() as session:
                        definition = lock_agent_definition(session, instance.agent_id)
                        if definition is None:
                            return
                        current = session.get(RuntimeInstance, instance.runtime_instance_id)
                        if current is not None and current.status not in {
                            "stopped",
                            "failed",
                            "lost",
                        }:
                            publish_unhealthy = current.status != "unhealthy"
                            current.status = "unhealthy"
                            current.last_error = (
                                f"Docker runtime observation failed ({type(exc).__name__})"
                            )
                            metadata = dict(current.runtime_metadata or {})
                            metadata["docker_observation_unavailable"] = utcnow().isoformat()
                            current.runtime_metadata = metadata
                    if publish_unhealthy:
                        await self.publish(
                            "runtime",
                            {
                                "runtime_instance_id": instance.runtime_instance_id,
                                "status": "unhealthy",
                            },
                        )
        if (
            instance.status == "starting"
            and instance.mode == "resident"
            and instance.control_url
            and not exited
        ):
            try:
                with self.database.session() as session:
                    definition = session.get(AgentDefinition, instance.agent_id)
                    token = self._agent_token(definition.snapshot) if definition else None
                is_ready = await self.external.ready(
                    instance.control_url,
                    adapter=instance.adapter,
                    token=token,
                )
            except RuntimeOperationError:
                is_ready = False
            if is_ready:
                publish_ready = False
                with self.database.session() as session:
                    definition = lock_agent_definition(session, instance.agent_id)
                    if definition is None:
                        return
                    current = session.get(RuntimeInstance, instance.runtime_instance_id)
                    if current is not None and current.status == "starting":
                        current.status = "ready"
                        current.ready_at = utcnow()
                        publish_ready = True
                if publish_ready:
                    await self.publish(
                        "runtime",
                        {
                            "runtime_instance_id": instance.runtime_instance_id,
                            "status": "ready",
                        },
                    )
            else:
                observation_succeeded = False
        if instance.last_heartbeat_at is not None and not exited:
            heartbeat_reference = ensure_aware(instance.last_heartbeat_at)
            if instance.adapter == "external":
                heartbeat_reference = max(heartbeat_reference, self.workspace_started_at)
            age = now - heartbeat_reference
            timeout = self._heartbeat_timeout(instance.agent_id)
            if age.total_seconds() > timeout:
                published_status: str | None = None
                with self.database.session() as session:
                    definition = lock_agent_definition(session, instance.agent_id)
                    if definition is None:
                        return
                    current = session.get(RuntimeInstance, instance.runtime_instance_id)
                    current_heartbeat = current.last_heartbeat_at if current is not None else None
                    still_expired = (
                        current_heartbeat is not None
                        and (now - ensure_aware(current_heartbeat)).total_seconds() > timeout
                    )
                    if (
                        current
                        and still_expired
                        and current.status not in {"stopped", "failed", "lost"}
                    ):
                        target = "lost" if current.adapter == "external" else "unhealthy"
                        if current.status != target:
                            current.status = target
                            published_status = target
                        if current.adapter == "external":
                            current.stopped_at = now
                if published_status is not None:
                    await self.publish(
                        "runtime",
                        {
                            "runtime_instance_id": instance.runtime_instance_id,
                            "status": published_status,
                        },
                    )
        if exited:
            if not await self._recover_runtime_outbox(instance):
                self._set_next_probe(instance.runtime_instance_id, succeeded=False)
                return
            await self._handle_exit(instance, exit_code)
            return
        self._set_next_probe(instance.runtime_instance_id, succeeded=observation_succeeded)

    def _set_next_probe(self, instance_id: str, *, succeeded: bool) -> None:
        """Persist deterministic bounded exponential observation backoff."""

        with self.database.session() as session:
            current = session.get(RuntimeInstance, instance_id)
            if current is None or current.status in {"stopped", "failed", "lost"}:
                return
            attempts = 0 if succeeded else min(30, int(current.probe_attempts) + 1)
            base = max(0.1, self.settings.scheduler.poll_interval_seconds)
            delay = (
                base
                if succeeded
                else min(
                    self.settings.scheduler.runtime_probe_backoff_max_seconds,
                    base * (2**attempts),
                )
            )
            current.probe_attempts = attempts
            current.next_probe_at = utcnow() + timedelta(seconds=delay)

    def _heartbeat_timeout(self, agent_id: str) -> float:
        with self.database.session() as session:
            definition = session.get(AgentDefinition, agent_id)
            if definition is None:
                return float(self.settings.scheduler.heartbeat_timeout_seconds)
            external = nested(definition.snapshot, "spec", "runtime", "external", default={}) or {}
            return float(
                external.get(
                    "heartbeat_timeout_seconds",
                    self.settings.scheduler.heartbeat_timeout_seconds,
                )
            )

    async def _handle_exit(self, instance: RuntimeInstance, exit_code: int | None) -> None:
        now = utcnow()
        run_ids_to_publish: list[str] = []
        run_id: str | None = None
        should_restart = False
        restart: dict[str, Any] = {}
        restart_attempt = 0
        agent_id = instance.agent_id
        with self.database.session() as session:
            definition = lock_agent_definition(session, agent_id)
            if definition is None:
                return
            current = session.get(RuntimeInstance, instance.runtime_instance_id)
            if current is None or current.status in {"stopped", "failed", "lost"}:
                return
            raw_run_id = (current.runtime_metadata or {}).get("ephemeral_run_id")
            run_id = raw_run_id if isinstance(raw_run_id, str) else None
            current.last_exit_code = exit_code
            current.stopped_at = now
            current.status = "stopped" if exit_code == 0 else "failed"
            if run_id:
                run = session.get(Run, run_id)
                if run and run.status not in {"succeeded", "failed", "cancelled", "timed_out"}:
                    run.status = "failed"
                    run.ended_at = now
                    if exit_code == 0:
                        error = {
                            "type": "runtime_missing_outcome",
                            "message": "ephemeral runtime exited without a terminal Run event",
                            "retryable": False,
                            "details": {},
                        }
                    else:
                        error = {
                            "type": "runtime_exit",
                            "message": f"process exited with {exit_code}",
                            "retryable": False,
                            "details": {"exit_code": exit_code},
                        }
                    self.storage.set_internal_run_error(run, error)
                    if not run.store_input:
                        run.input = None
                    run_ids_to_publish.append(run.run_id)
            else:
                run_ids_to_publish.extend(
                    self._fail_assigned_runs(
                        session,
                        instance_id=current.runtime_instance_id,
                        exit_code=exit_code,
                        message="resident runtime exited before assigned Run completion",
                    )
                )
                restart = (
                    nested(definition.snapshot, "spec", "runtime", "restart", default={})
                    if definition
                    else {}
                )
                policy = (restart or {}).get("policy", "never")
                should_restart = policy == "always" or (policy == "on_failure" and exit_code != 0)
                maximum = int((restart or {}).get("max_attempts", 0))
                reset_after = float((restart or {}).get("reset_after_seconds", 300))
                uptime = (
                    (now - ensure_aware(current.started_at)).total_seconds()
                    if current.started_at is not None
                    else 0
                )
                if uptime >= reset_after:
                    current.restart_attempts = 0
                    current.consecutive_failures = 0
                else:
                    current.consecutive_failures = current.restart_attempts + 1
                if current.restart_attempts >= maximum:
                    should_restart = False
                    current.status = "failed"
                    current.last_error = "restart policy reached max_attempts"
                restart_attempt = current.restart_attempts + 1
                agent_id = current.agent_id
        await self.publish(
            "runtime",
            {"runtime_instance_id": instance.runtime_instance_id, "status": current.status},
        )
        for failed_run_id in run_ids_to_publish:
            await self.publish("run", {"run_id": failed_run_id, "status": "failed"})
        if should_restart:
            base = float(restart.get("backoff_seconds", 1))
            maximum_backoff = float(restart.get("max_backoff_seconds", 300))
            delay = min(maximum_backoff, base * (2 ** max(0, restart_attempt - 1)))
            self._schedule_restart(agent_id, restart_attempt, delay)

        cleanup_complete = True
        if instance.adapter == "process":
            await self.process.cleanup(instance.runtime_instance_id)
        if instance.adapter == "docker" and instance.container_id:
            cleanup_complete = await self._cleanup_container(
                instance.runtime_instance_id, instance.container_id
            )
        if cleanup_complete:
            self._remove_runtime_outbox(instance.runtime_instance_id)

    def _schedule_restart_after_failure(
        self,
        agent_id: str,
        snapshot: dict[str, Any],
        *,
        restart_attempt: int,
    ) -> None:
        restart = nested(snapshot, "spec", "runtime", "restart", default={}) or {}
        policy = restart.get("policy", "never")
        maximum = int(restart.get("max_attempts", 0))
        if policy not in {"always", "on_failure"}:
            return
        if restart_attempt >= maximum:
            with self.database.session() as session:
                latest = session.scalar(
                    select(RuntimeInstance)
                    .where(RuntimeInstance.agent_id == agent_id)
                    .order_by(RuntimeInstance.started_at.desc())
                )
                if latest is not None:
                    metadata = dict(latest.runtime_metadata or {})
                    metadata["restart_exhausted"] = True
                    latest.runtime_metadata = metadata
                    if restart_attempt >= maximum and maximum > 0:
                        latest.last_error = "restart policy reached max_attempts"
            return
        next_attempt = restart_attempt + 1
        base = float(restart.get("backoff_seconds", 1))
        maximum_backoff = float(restart.get("max_backoff_seconds", 300))
        delay = min(maximum_backoff, base * (2 ** max(0, next_attempt - 1)))
        self._schedule_restart(agent_id, next_attempt, delay)

    def _schedule_restart(self, agent_id: str, restart_attempt: int, delay: float) -> None:
        current_task = asyncio.current_task()
        existing = self._restart_by_agent.get(agent_id)
        if existing is not None and not existing.done() and existing is not current_task:
            return
        self.telemetry.runtime_restarts.add(1, {"agent_id": agent_id})
        task = asyncio.create_task(
            self._restart_after(agent_id, restart_attempt, delay),
            name=f"kitsune-restart-{agent_id}-{restart_attempt}",
        )
        self._restart_by_agent[agent_id] = task
        self._restart_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._restart_tasks.discard(done)
            if self._restart_by_agent.get(agent_id) is done:
                self._restart_by_agent.pop(agent_id, None)

        task.add_done_callback(completed)

    async def _cleanup_container(self, instance_id: str, container_id: str) -> bool:
        """Remove a terminal container and persist completion for retry-safe cleanup."""

        removed = await self.docker.archive_and_remove(container_id)
        if removed:
            with self.database.session() as session:
                instance = session.get(RuntimeInstance, instance_id)
                if instance is not None:
                    instance.container_removed_at = utcnow()
        return removed

    async def cleanup_terminal_containers(self) -> int:
        """Retry removal for every terminal Docker container not yet acknowledged."""

        with self.database.session() as session:
            pending = [
                (item.runtime_instance_id, item.container_id)
                for item in session.scalars(
                    select(RuntimeInstance)
                    .where(
                        RuntimeInstance.adapter == "docker",
                        RuntimeInstance.container_id.is_not(None),
                        RuntimeInstance.container_removed_at.is_(None),
                        RuntimeInstance.status.in_(["stopped", "failed", "lost"]),
                    )
                    .order_by(RuntimeInstance.stopped_at, RuntimeInstance.runtime_instance_id)
                    .limit(self.settings.scheduler.runtime_probe_batch_size)
                )
            ]
        semaphore = asyncio.Semaphore(self.settings.scheduler.runtime_probe_concurrency)

        async def cleanup(instance_id: str, container_id: str) -> bool:
            async with semaphore:
                if not await self._cleanup_container(instance_id, container_id):
                    return False
                self._remove_runtime_outbox(instance_id)
                return True

        results = await asyncio.gather(
            *(
                cleanup(instance_id, container_id)
                for instance_id, container_id in pending
                if container_id
            )
        )
        return sum(results)

    async def _restart_after(self, agent_id: str, restart_attempt: int, delay: float) -> None:
        """Wait without blocking health monitoring, then recheck desired state and restart."""

        await asyncio.sleep(delay)
        with self.database.session() as session:
            definition = session.get(AgentDefinition, agent_id)
            if (
                definition is None
                or not definition.active
                or definition.desired_state != "running"
                or definition.runtime_mode != "resident"
                or definition.runtime_adapter == "external"
            ):
                return
        try:
            await self.start_agent(agent_id, restart_attempt=restart_attempt)
        except Exception:
            # start_agent persists and publishes the concrete Runtime Instance failure.
            return

    async def shutdown(self) -> None:
        """Gracefully stop Workspace-owned resident processes and containers."""

        await self._cancel_restart_tasks()

        with self.database.session() as session:
            managed_ids = list(
                session.scalars(
                    select(RuntimeInstance.runtime_instance_id).where(
                        RuntimeInstance.adapter.in_(["process", "docker"]),
                        RuntimeInstance.status.in_(
                            ["pending", "starting", "ready", "unhealthy", "stopping"]
                        ),
                    )
                )
            )
        for instance_id in managed_ids:
            with contextlib.suppress(RuntimeOperationError):
                await self.stop_instance(instance_id)
        await self.process.close()

    async def abandon(self) -> None:
        """Forget local runtime tasks after lease loss without touching owned lifecycles."""

        await self._cancel_restart_tasks()
        await self.process.close()
        self.docker._redactions.clear()

    async def _cancel_restart_tasks(self) -> None:
        """Cancel every locally scheduled restart without mutating runtime state."""

        restart_tasks = list(self._restart_tasks)
        for task in restart_tasks:
            task.cancel()
        if restart_tasks:
            await asyncio.gather(*restart_tasks, return_exceptions=True)
        self._restart_by_agent.clear()
