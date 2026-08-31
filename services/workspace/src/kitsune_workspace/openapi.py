"""Deterministic OpenAPI export without starting a server or touching the database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import create_app
from .config import WorkspaceSettings


def export_openapi(output: Path, settings: WorkspaceSettings | None = None) -> None:
    """Write the canonical Workspace OpenAPI document with stable formatting."""

    application = create_app(settings)
    try:
        document = application.openapi()
    finally:
        application.state.control.telemetry.shutdown()
        application.state.database.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Run the standalone OpenAPI exporter."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args()
    settings = WorkspaceSettings.load(arguments.config) if arguments.config else WorkspaceSettings()
    export_openapi(arguments.output, settings)


if __name__ == "__main__":
    main()
