"""Installed CLI migration and packaged-resource regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from typer.testing import CliRunner

from kitsune_workspace.cli import workspace_app
from kitsune_workspace.database import CURRENT_SCHEMA_REVISION, Database
from kitsune_workspace.models import Base


def test_unmigrated_database_error_names_the_packaged_operator_command(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    try:
        with pytest.raises(
            RuntimeError,
            match=r"kitsune workspace migrate --config <path>",
        ):
            database.require_migrated_schema()
    finally:
        database.dispose()


def test_migrate_command_uses_configured_database_and_packaged_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migrated.sqlite3"
    manifest_directory = tmp_path / "agents"
    manifest_directory.mkdir()
    config = tmp_path / "workspace.toml"
    config.write_text(
        "\n".join(
            [
                "[workspace]",
                'bind = "127.0.0.1:8080"',
                f'database_url = "sqlite:///{database_path}"',
                f'agent_manifest_directory = "{manifest_directory}"',
            ]
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(workspace_app, ["migrate", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert CURRENT_SCHEMA_REVISION in result.output

    database = Database(f"sqlite:///{database_path}")
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            *Base.metadata.tables,
            "alembic_version",
        }
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                CURRENT_SCHEMA_REVISION
            )
    finally:
        database.dispose()
