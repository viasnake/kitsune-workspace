"""Strict nested configuration and mandatory singleton-lock regressions."""

from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import ManifestFactory, settings_for
from pydantic import ValidationError

from kitsune_workspace import database as database_module
from kitsune_workspace.config import WorkspaceSettings
from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database
from kitsune_workspace.util import utcnow


@pytest.mark.parametrize(
    "section",
    ["workspace", "auth", "events", "scheduler", "security", "observability"],
)
def test_every_nested_settings_section_rejects_unknown_keys(section: str) -> None:
    with pytest.raises(ValidationError) as failure:
        WorkspaceSettings.model_validate({section: {"misspelled_setting": True}})

    assert (section, "misspelled_setting") in {
        tuple(error["loc"]) for error in failure.value.errors()
    }


def test_removed_instance_lock_bypass_is_rejected() -> None:
    with pytest.raises(ValidationError) as failure:
        WorkspaceSettings.model_validate({"workspace": {"instance_lock": False}})

    assert ("workspace", "instance_lock") in {
        tuple(error["loc"]) for error in failure.value.errors()
    }


def test_empty_custom_redacted_keys_cannot_disable_mandatory_baseline() -> None:
    settings = WorkspaceSettings.model_validate({"security": {"redacted_keys": []}})

    assert {
        "authorization",
        "cookie",
        "api_key",
        "private_key",
        "password",
        "secret",
        "token",
        "credential",
    } <= settings.security.redacted_keys


def test_nested_environment_typo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITSUNE_SCHEDULER__POLL_INTERVL_SECONDS", "2")

    with pytest.raises(ValidationError) as failure:
        WorkspaceSettings()

    assert ("scheduler", "poll_intervl_seconds") in {
        tuple(error["loc"]) for error in failure.value.errors()
    }


def test_current_compose_settings_remain_valid() -> None:
    repository = Path(__file__).parents[3]
    settings = WorkspaceSettings.from_toml(repository / "deploy/workspace.compose.toml")

    assert settings.workspace.name == "compose"
    assert settings.security.docker_outbox_archive_max_members == 64
    assert settings.security.sse_max_connections_per_principal == 5


def test_non_loopback_http_agent_url_requires_its_dedicated_opt_in() -> None:
    with pytest.raises(ValidationError, match="allow_insecure_agent_network"):
        WorkspaceSettings.model_validate(
            {"workspace": {"agent_url": "http://agent-gateway.internal:18081"}}
        )

    settings = WorkspaceSettings.model_validate(
        {
            "workspace": {"agent_url": "http://agent-gateway.internal:18081"},
            "security": {"allow_insecure_agent_network": True},
        }
    )
    assert settings.workspace.agent_url == "http://agent-gateway.internal:18081"


@pytest.mark.parametrize(
    "agent_url",
    [
        "http://agent.internal:18081/path",
        "http://user@agent.internal:18081",
        "http://agent.internal:18081?route=full",
    ],
)
def test_agent_url_is_a_credential_free_root(agent_url: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(
            {
                "workspace": {"agent_url": agent_url},
                "security": {"allow_insecure_agent_network": True},
            }
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_workspace_sqlite_forces_private_directory_database_wal_and_shm_modes(
    tmp_path: Path,
) -> None:
    """Workspace persistence remains private even under a permissive process umask."""

    path = tmp_path / "private-workspace" / "workspace.sqlite3"
    previous_umask = os.umask(0)
    try:
        database = Database(f"sqlite:///{path}")
        database.create_schema()
    finally:
        os.umask(previous_umask)

    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            assert candidate.exists(), candidate
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    finally:
        database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_workspace_sqlite_rejects_missing_nested_parent_without_creating_shared_directories(
    tmp_path: Path,
) -> None:
    """Workspace must not let a permissive umask expose auto-created ancestors."""

    missing_ancestor = tmp_path / "missing-ancestor"
    path = missing_ancestor / "private-workspace" / "workspace.sqlite3"
    previous_umask = os.umask(0)
    try:
        with pytest.raises(RuntimeError, match="parent must already exist"):
            Database(f"sqlite:///{path}")
    finally:
        os.umask(previous_umask)

    assert not missing_ancestor.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX path security is required")
def test_workspace_sqlite_validates_existing_ancestor_before_creating_private_directory(
    tmp_path: Path,
) -> None:
    """The one permitted parent must not be writable by others or be a symlink."""

    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir(mode=0o700)
    shared_parent.chmod(0o777)
    private_path = shared_parent / "private-workspace" / "workspace.sqlite3"

    with pytest.raises(RuntimeError, match=r"parent.*group/world-writable"):
        Database(f"sqlite:///{private_path}")

    assert not private_path.parent.exists()
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target_parent, target_is_directory=True)
    linked_path = linked_parent / "private-workspace" / "workspace.sqlite3"

    with pytest.raises(RuntimeError, match="parent must be a non-symlink directory"):
        Database(f"sqlite:///{linked_path}")

    assert not (target_parent / "private-workspace").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are platform-specific")
def test_workspace_sqlite_rejects_symlink_directory_and_database(tmp_path: Path) -> None:
    """Workspace refuses SQLite locations that redirect through a final symlink."""

    target_directory = tmp_path / "target"
    target_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(target_directory, target_is_directory=True)
    with pytest.raises(RuntimeError, match="non-symlink directory"):
        Database(f"sqlite:///{linked_directory / 'workspace.sqlite3'}")

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    target_file = tmp_path / "target.sqlite3"
    target_file.touch(mode=0o600)
    linked_file = private_directory / "workspace.sqlite3"
    linked_file.symlink_to(target_file)
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        Database(f"sqlite:///{linked_file}")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_workspace_sqlite_never_chmods_preexisting_or_shared_parent_directories(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "caller-owned"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    before = stat.S_IMODE(parent.stat().st_mode)

    database = Database(f"sqlite:///{parent / 'workspace.sqlite3'}")
    try:
        assert stat.S_IMODE(parent.stat().st_mode) == before == 0o755
        assert stat.S_IMODE((parent / "workspace.sqlite3").stat().st_mode) == 0o600
    finally:
        database.dispose()
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir()
    shared_directory.chmod(0o777)
    workspace_mode = stat.S_IMODE(shared_directory.stat().st_mode)
    with pytest.raises(RuntimeError, match="group/world-writable"):
        database_module._require_private_directory(shared_directory)
    assert stat.S_IMODE(shared_directory.stat().st_mode) == workspace_mode
    shared_temporary = Path("/tmp")  # noqa: S108 - assert the real shared parent is untouched
    temporary_mode = stat.S_IMODE(shared_temporary.stat().st_mode)
    with pytest.raises(RuntimeError, match=r"not owned|group/world-writable"):
        database_module._require_private_directory(shared_temporary)
    assert stat.S_IMODE(shared_temporary.stat().st_mode) == temporary_mode


@pytest.mark.asyncio
async def test_control_plane_always_acquires_and_enforces_singleton_lock(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    first = ControlPlane(database, settings)
    second = ControlPlane(database, settings)

    try:
        assert not first.accepting_operations
        await first.start()
        assert first.accepting_operations
        assert first.instance_lock.acquired

        with pytest.raises(RuntimeError, match="instance lock is held"):
            await second.start()
        assert not second.accepting_operations

        await first.stop()
        assert not first.accepting_operations
        await second.start()
        assert second.accepting_operations
    finally:
        if first.instance_lock.acquired:
            await first.stop()
        if second.instance_lock.acquired:
            await second.stop()
        else:
            second.telemetry.shutdown()
        database.dispose()


@pytest.mark.asyncio
async def test_lost_lease_stop_abandons_local_runtime_without_lifecycle_shutdown(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    database = Database(settings.workspace.database_url)
    database.create_schema()
    control = ControlPlane(database, settings)
    control.instance_lock.acquired = True
    control.instance_lock.confirmed_expires_at = utcnow() - timedelta(seconds=1)
    control.runtime.shutdown = AsyncMock()
    control.runtime.abandon = AsyncMock()

    await control.stop()

    control.runtime.shutdown.assert_not_awaited()
    control.runtime.abandon.assert_awaited_once_with()
    assert not control.instance_lock.acquired
    database.dispose()
