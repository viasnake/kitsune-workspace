"""Exercise the Workspace CLI exposed by an isolated installed wheel."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """Run the installed command and migrate a fresh SQLite database."""

    executable = shutil.which("kitsune")
    assert executable is not None, "the installed kitsune executable is not on PATH"
    subprocess.run([executable, "workspace", "--help"], check=True)

    with tempfile.TemporaryDirectory(prefix="kitsune-workspace-wheel-") as temporary:
        root = Path(temporary)
        database = root / "workspace.sqlite3"
        config = root / "workspace.toml"
        config.write_text(
            "\n".join(
                (
                    "[workspace]",
                    f'database_url = "sqlite:///{database}"',
                    f'agent_manifest_directory = "{root / "agents"}"',
                    f'runtime_state_directory = "{root / "runtime"}"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [executable, "workspace", "migrate", "--config", str(config)],
            check=True,
        )
        with sqlite3.connect(database) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision == ("0001_workspace_schema",)


if __name__ == "__main__":
    main()
