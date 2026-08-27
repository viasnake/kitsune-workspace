"""Atomic Agent Manifest validation, snapshotting, and schedule materialization."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import CroniterBadCronError, croniter
from pydantic import AnyUrl, BaseModel, SecretStr, ValidationError
from sqlalchemy import delete, select

from .config import WorkspaceSettings
from .database import Database
from .models import AgentDefinition, AgentStorageUsage, Schedule, Trigger
from .util import AGENT_ID_PATTERN, nested, utcnow

_MANAGED_OUTBOX_MOUNT = Path("/var/lib/kitsune-outbox")
_DOCKER_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DOCKER_RESERVED_NETWORKS = frozenset({"bridge", "default", "host", "none"})


class ManifestReloadError(ValueError):
    """Raised when any file makes an atomic Manifest reload invalid."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("Agent Manifest reload rejected")


@dataclass(frozen=True)
class ValidatedManifest:
    """Canonical validated manifest and persistence metadata."""

    agent_id: str
    path: Path
    snapshot: dict[str, Any]
    content_hash: str


def _safe_validation_issues(exc: Exception) -> list[dict[str, Any]]:
    """Return structural validation metadata without echoing manifest-controlled values."""

    if not isinstance(exc, ValidationError):
        return [
            {
                "loc": [],
                "type": type(exc).__name__,
                "msg": "Manifest validation failed",
            }
        ]
    issues: list[dict[str, Any]] = []
    for error in exc.errors():
        error_type = str(error.get("type", "validation_error"))
        location: list[Any] = list(error.get("loc", ()))
        if error_type == "missing":
            message = "Required manifest field is missing"
        elif error_type == "extra_forbidden":
            message = "Manifest field is not supported"
            if location:
                location[-1] = "<extra-field>"
        else:
            message = "Manifest value is invalid"
        issues.append(
            {
                "loc": location,
                "type": error_type,
                "msg": message,
            }
        )
    return issues


def _contract_validate(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        from kitsune_contracts import AgentManifest
    except ImportError as exc:
        raise RuntimeError("kitsune-contracts must be installed with Workspace") from exc
    manifest = AgentManifest.model_validate(raw)
    return _contract_json(manifest)


def _contract_json(value: Any) -> Any:
    """Serialize validated contracts while preserving secret reference strings."""

    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return {
            key: _contract_json(item)
            for key, item in value.model_dump(
                mode="python", by_alias=True, exclude_none=True
            ).items()
        }
    if isinstance(value, dict):
        return {str(key): _contract_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_contract_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Path, AnyUrl)):
        return str(value)
    return value


def _validate_secret_reference(reference: Any, field: str) -> None:
    if not isinstance(reference, str):
        raise ValueError(f"{field} must be an env:// or file:// reference")
    if reference.startswith("env://") and reference.removeprefix("env://"):
        return
    if reference.startswith("file://") and Path(reference.removeprefix("file://")).is_absolute():
        return
    raise ValueError(f"{field} must be an env:// name or absolute file:// path")


def _validate_managed_control_url(value: Any, *, loopback_only: bool) -> None:
    if not isinstance(value, str):
        raise ValueError("resident managed runtime requires a typed control_url")
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.netloc:
        raise ValueError("managed control_url must be an absolute HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("managed control_url cannot contain credentials, query, or fragment")
    if parsed.path.rstrip("/"):
        raise ValueError("managed control_url must identify the Control API root")
    if not loopback_only:
        return
    hostname = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.casefold() == "localhost"
    if not loopback:
        raise ValueError("resident process control_url must use a loopback host")


def _validate_runtime_security(snapshot: dict[str, Any], settings: WorkspaceSettings) -> None:
    runtime = nested(snapshot, "spec", "runtime", default={})
    adapter = runtime.get("adapter")
    resident = runtime.get("mode") == "resident"
    if adapter == "process":
        process = runtime.get("process") or {}
        command = process.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) for x in command)
        ):
            raise ValueError("process adapter requires a non-empty string command list")
        if resident:
            _validate_managed_control_url(process.get("control_url"), loopback_only=True)
    elif adapter == "docker":
        docker = runtime.get("docker") or {}
        image = docker.get("image")
        if not isinstance(image, str) or not image or len(image) > 255:
            raise ValueError("docker adapter requires an image")
        command = docker.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or len(command) > 256
            or not all(isinstance(part, str) and 1 <= len(part) <= 4096 for part in command)
        ):
            raise ValueError("docker.command must be a non-empty bounded string list")
        working_directory = docker.get("working_directory")
        if working_directory is not None:
            if not isinstance(working_directory, str):
                raise ValueError("docker.working_directory must be an absolute POSIX path")
            working_path = PurePosixPath(working_directory)
            if (
                not working_path.is_absolute()
                or working_directory.startswith("//")
                or ".." in working_path.parts
                or working_path.as_posix() != working_directory
                or len(working_directory) > 4096
            ):
                raise ValueError(
                    "docker.working_directory must be a normalized absolute POSIX path"
                )
        if docker.get("privileged"):
            raise ValueError("privileged Docker containers are prohibited")
        if docker.get("host_network"):
            raise ValueError("Docker host networking is prohibited")
        network = docker.get("network")
        if not isinstance(network, str):
            raise ValueError("docker adapter requires an explicit isolated network")
        normalized_network = network.strip()
        folded_network = normalized_network.casefold()
        if (
            not normalized_network
            or len(normalized_network) > 128
            or folded_network in _DOCKER_RESERVED_NETWORKS
            or folded_network.startswith(("container:", "service:"))
            or ":" in normalized_network
            or "/" in normalized_network
            or _DOCKER_NETWORK_NAME.fullmatch(normalized_network) is None
        ):
            raise ValueError("Docker network must name an explicit isolated bridge network")
        control_port = docker.get("control_port")
        if resident and not isinstance(control_port, int):
            raise ValueError("resident Docker Runtime requires docker.control_port")
        if not resident and control_port is not None:
            raise ValueError("ephemeral Docker Runtime cannot declare docker.control_port")
        if control_port is not None and not 1024 <= control_port <= 65535:
            raise ValueError("docker.control_port must be between 1024 and 65535")
        memory_limit = docker.get("memory_limit_bytes")
        cpu_limit = docker.get("cpu_limit")
        pids_limit = docker.get("pids_limit")
        if (
            not isinstance(memory_limit, int)
            or isinstance(memory_limit, bool)
            or not 67_108_864 <= memory_limit <= 68_719_476_736
        ):
            raise ValueError("docker.memory_limit_bytes must be between 64 MiB and 64 GiB")
        if (
            not isinstance(cpu_limit, int | float)
            or isinstance(cpu_limit, bool)
            or not math.isfinite(float(cpu_limit))
            or not 0.01 <= float(cpu_limit) <= 64
        ):
            raise ValueError("docker.cpu_limit must be finite and between 0.01 and 64")
        if (
            not isinstance(pids_limit, int)
            or isinstance(pids_limit, bool)
            or not 16 <= pids_limit <= 32_768
        ):
            raise ValueError("docker.pids_limit must be between 16 and 32768")
        for volume in docker.get("volumes", []):
            if not isinstance(volume, str):
                raise ValueError("Docker volumes must be manifest-declared bind strings")
            parts = volume.split(":")
            if len(parts) not in {2, 3}:
                raise ValueError("Docker volumes must use absolute-host:absolute-container[:ro|rw]")
            if not Path(parts[0]).is_absolute() or not Path(parts[1]).is_absolute():
                raise ValueError("Docker bind mount host and container paths must be absolute")
            if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
                raise ValueError("Docker bind mount mode must be ro or rw")
            docker_socket = Path("/var/run/docker.sock").resolve(strict=False)
            resolved_source = Path(parts[0]).resolve(strict=False)
            container_mount = PurePosixPath(parts[1])
            if ".." in container_mount.parts:
                raise ValueError("Docker volume container paths cannot contain dot-dot segments")
            if (
                resolved_source == docker_socket
                or resolved_source in docker_socket.parents
                or container_mount == PurePosixPath("/var/run/docker.sock")
            ):
                raise ValueError("Docker volumes must not expose the Docker Engine socket")
            container_path = PurePosixPath(parts[1])
            managed_outbox = PurePosixPath(_MANAGED_OUTBOX_MOUNT.as_posix())
            if (
                container_path == managed_outbox
                or container_path in managed_outbox.parents
                or managed_outbox in container_path.parents
            ):
                raise ValueError("Docker volumes cannot overlap the Workspace-managed outbox mount")
    elif adapter == "external":
        external = runtime.get("external") or {}
        endpoint = external.get("control_url") or external.get("url") or external.get("endpoint")
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme != "https" and not settings.security.allow_insecure_external_agents:
                raise ValueError("external Agent endpoints must use HTTPS")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("external Agent endpoint must be an absolute HTTP(S) URL")
    else:
        raise ValueError(f"unsupported runtime adapter: {adapter!r}")

    for section in ("environment", "secrets"):
        references = runtime.get(section) or {}
        if not isinstance(references, dict):
            raise ValueError(f"runtime.{section} must be a mapping")
        for key, reference in references.items():
            _validate_secret_reference(reference, f"runtime.{section}.{key}")


def _validate_triggers(snapshot: dict[str, Any]) -> None:
    triggers = nested(snapshot, "spec", "triggers", default=[]) or []
    seen: set[str] = set()
    for trigger in triggers:
        trigger_id = trigger.get("id")
        if not trigger_id or trigger_id in seen:
            raise ValueError(f"trigger IDs must be non-empty and unique: {trigger_id!r}")
        seen.add(trigger_id)
        if trigger.get("type") == "schedule":
            expression = trigger.get("cron")
            timezone_name = trigger.get("timezone", "UTC")
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown schedule timezone: {timezone_name}") from exc
            try:
                croniter(expression, datetime.now(ZoneInfo(timezone_name)))
            except (CroniterBadCronError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid cron expression for trigger {trigger_id}: {expression!r}"
                ) from exc
        if trigger.get("type") == "webhook":
            secret_ref = trigger.get("shared_secret_ref") or trigger.get("hmac_secret_ref")
            if not secret_ref:
                raise ValueError(f"webhook trigger {trigger_id} requires a secret reference")
            _validate_secret_reference(secret_ref, f"trigger.{trigger_id}.secret_ref")


def _validate_file(path: Path, settings: WorkspaceSettings) -> ValidatedManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("manifest file could not be parsed") from exc
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be a mapping")
    snapshot = _contract_validate(raw)
    agent_id = nested(snapshot, "metadata", "id")
    if not isinstance(agent_id, str) or not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise ValueError("metadata.id must be a lowercase DNS label")
    _validate_runtime_security(snapshot, settings)
    _validate_triggers(snapshot)
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ValidatedManifest(
        agent_id=agent_id,
        path=path.resolve(),
        snapshot=snapshot,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def next_fire_time(expression: str, timezone_name: str, after: datetime | None = None) -> datetime:
    """Return the next cron occurrence as UTC."""

    zone = ZoneInfo(timezone_name)
    base = (after or utcnow()).astimezone(zone)
    result = croniter(expression, base).get_next(datetime)
    if result.tzinfo is None:
        result = result.replace(tzinfo=zone)
    return result.astimezone(UTC)


class ManifestRegistry:
    """Validate every Manifest before applying one atomic registry transaction."""

    def __init__(self, database: Database, settings: WorkspaceSettings) -> None:
        self.database = database
        self.settings = settings

    def validate_directory(self) -> list[ValidatedManifest]:
        """Validate all YAML files without touching persistent state."""

        directory = self.settings.workspace.agent_manifest_directory
        files = sorted({*directory.glob("*.yaml"), *directory.glob("*.yml")})
        errors: list[dict[str, Any]] = []
        manifests: list[ValidatedManifest] = []
        seen: dict[str, Path] = {}
        if not directory.is_dir():
            errors.append({"path": str(directory), "error": "manifest directory does not exist"})
        for path in files:
            try:
                manifest = _validate_file(path, self.settings)
                if manifest.agent_id in seen:
                    raise ValueError(
                        f"duplicate agent ID also declared by {seen[manifest.agent_id]}"
                    )
                seen[manifest.agent_id] = path
                manifests.append(manifest)
            except (ValueError, ValidationError, RuntimeError) as exc:
                errors.append({"path": str(path), "error": _safe_validation_issues(exc)})
        if errors:
            raise ManifestReloadError(errors)
        return manifests

    def reload(self) -> dict[str, Any]:
        """Atomically apply snapshots while preserving unchanged schedule cursors."""

        manifests = self.validate_directory()
        now = utcnow()
        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        removed: list[str] = []
        incoming_ids = {manifest.agent_id for manifest in manifests}
        with self.database.session() as session:
            existing = {
                item.agent_id: item for item in session.scalars(select(AgentDefinition)).all()
            }
            for agent_id, definition in existing.items():
                if agent_id not in incoming_ids and definition.active:
                    definition.active = False
                    definition.desired_state = "stopped"
                    session.execute(delete(Trigger).where(Trigger.agent_id == agent_id))
                    session.execute(delete(Schedule).where(Schedule.agent_id == agent_id))
                    removed.append(agent_id)

            for manifest in manifests:
                snapshot = manifest.snapshot
                metadata = snapshot["metadata"]
                runtime = snapshot["spec"]["runtime"]
                definition = existing.get(manifest.agent_id)
                if definition is None:
                    definition = AgentDefinition(agent_id=manifest.agent_id)
                    session.add(definition)
                    session.add(AgentStorageUsage(agent_id=manifest.agent_id))
                    added.append(manifest.agent_id)
                elif definition.content_hash == manifest.content_hash and definition.active:
                    unchanged.append(manifest.agent_id)
                    continue
                else:
                    updated.append(manifest.agent_id)
                definition.revision = snapshot["revision"]
                definition.display_name = metadata.get("display_name", manifest.agent_id)
                definition.description = metadata.get("description")
                definition.labels = metadata.get("labels", {})
                definition.runtime_adapter = runtime["adapter"]
                definition.runtime_mode = runtime["mode"]
                definition.desired_state = runtime.get("desired_state", "running")
                definition.active = True
                definition.snapshot = snapshot
                definition.content_hash = manifest.content_hash
                definition.source_path = str(manifest.path)
                definition.loaded_at = now

                current_triggers = {
                    item.trigger_id: item
                    for item in session.scalars(
                        select(Trigger).where(Trigger.agent_id == manifest.agent_id)
                    )
                }
                current_schedules = {
                    item.trigger_id: item
                    for item in session.scalars(
                        select(Schedule).where(Schedule.agent_id == manifest.agent_id)
                    )
                }
                desired_ids = {
                    item["id"] for item in nested(snapshot, "spec", "triggers", default=[]) or []
                }
                for trigger_id, trigger in current_triggers.items():
                    if trigger_id not in desired_ids:
                        session.delete(trigger)
                for trigger_id, schedule in current_schedules.items():
                    if trigger_id not in desired_ids:
                        session.delete(schedule)
                for trigger_data in nested(snapshot, "spec", "triggers", default=[]) or []:
                    trigger = current_triggers.get(trigger_data["id"])
                    if trigger is None:
                        trigger = Trigger(
                            agent_id=manifest.agent_id,
                            trigger_id=trigger_data["id"],
                        )
                        session.add(trigger)
                    trigger.type = trigger_data["type"]
                    trigger.handler = trigger_data["handler"]
                    trigger.configuration = trigger_data
                    if trigger_data["type"] == "schedule":
                        timezone_name = trigger_data.get("timezone", "UTC")
                        schedule = current_schedules.get(trigger_data["id"])
                        cursor_changed = (
                            schedule is None
                            or schedule.cron != trigger_data["cron"]
                            or schedule.timezone != timezone_name
                        )
                        if schedule is None:
                            schedule = Schedule(
                                agent_id=manifest.agent_id,
                                trigger_id=trigger_data["id"],
                            )
                            session.add(schedule)
                        schedule.handler = trigger_data["handler"]
                        schedule.cron = trigger_data["cron"]
                        schedule.timezone = timezone_name
                        schedule.overlap = trigger_data.get("overlap", "skip")
                        schedule.misfire_grace_seconds = trigger_data.get(
                            "misfire_grace_seconds", 300
                        )
                        schedule.enabled = trigger_data.get("enabled", True)
                        if cursor_changed:
                            schedule.next_fire_at = next_fire_time(
                                trigger_data["cron"], timezone_name, now
                            )
                            schedule.last_fire_at = None
                            schedule.last_outcome = None
                    elif schedule := current_schedules.get(trigger_data["id"]):
                        session.delete(schedule)
        return {
            "loaded": len(manifests),
            "added": sorted(added),
            "updated": sorted(updated),
            "removed": sorted(removed),
            "unchanged": sorted(unchanged),
            "loaded_at": now.isoformat(),
        }
