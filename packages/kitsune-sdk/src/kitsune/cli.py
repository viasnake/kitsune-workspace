"""The unified ``kitsune`` command for Agent, Manifest, and optional Workspace operations."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer
from kitsune_contracts import RuntimeMode, load_manifest, manifest_json_schema

from .app import KitsuneApp
from .outbox import (
    EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE,
    EphemeralDeliveryIncompleteError,
)

app = typer.Typer(help="Kitsune Agent Application and Workspace operations.", no_args_is_help=True)
agent_app = typer.Typer(help="Run a resident or one-shot Agent Application.", no_args_is_help=True)
manifest_app = typer.Typer(
    help="Validate and inspect the Agent Manifest contract.", no_args_is_help=True
)
app.add_typer(agent_app, name="agent")
app.add_typer(manifest_app, name="manifest")


def _attach_workspace_commands() -> None:
    try:
        module = importlib.import_module("kitsune_workspace.cli")
    except ModuleNotFoundError as exc:
        if exc.name not in {"kitsune_workspace", "kitsune_workspace.cli"}:
            raise
        return
    workspace_app = getattr(module, "workspace_app", None)
    if not isinstance(workspace_app, typer.Typer):
        raise TypeError("kitsune_workspace.cli.workspace_app must be a Typer application")
    app.add_typer(workspace_app, name="workspace")


_attach_workspace_commands()


@agent_app.command("serve")
def serve_agent(
    application: Annotated[str, typer.Argument(help="Import reference in module:attribute form")],
    host: Annotated[str | None, typer.Option(help="Control API bind host")] = None,
    port: Annotated[int | None, typer.Option(help="Control API bind port")] = None,
) -> None:
    """Serve one resident Agent Application's asynchronous Control API."""

    loaded = _load_application(application)
    loaded.run(host=host, port=port)


@agent_app.command("once")
def run_once(
    application: Annotated[str, typer.Argument(help="Import reference in module:attribute form")],
    handler: Annotated[
        str | None, typer.Option("--handler", help="Registered Handler name")
    ] = None,
    input_path: Annotated[
        str | None, typer.Option("--input", help="JSON file path, or '-' for standard input")
    ] = None,
) -> None:
    """Execute one Handler, a managed ephemeral Run, or persisted-outbox recovery."""

    loaded = _load_application(application)
    drain_only = os.getenv("KITSUNE_OUTBOX_DRAIN_ONLY", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    managed_ephemeral = bool(os.getenv("KITSUNE_RUN_ID"))
    if not drain_only and not managed_ephemeral and (handler is None or input_path is None):
        raise typer.BadParameter(
            "--handler and --input are required unless KITSUNE_RUN_ID selects "
            "managed ephemeral mode"
        )

    async def execute() -> None:
        if drain_only:
            await loaded.drain_outbox_only()
            return
        await loaded.startup(mode=RuntimeMode.EPHEMERAL)
        try:
            if managed_ephemeral:
                result = await loaded.run_ephemeral_from_environment()
            else:
                assert handler is not None and input_path is not None
                content = (
                    sys.stdin.read()
                    if input_path == "-"
                    else await anyio.Path(input_path).read_text(encoding="utf-8")
                )
                input_data: Any = json.loads(content)
                result = await loaded.execute(handler, input_data)
            typer.echo(result.model_dump_json())
        finally:
            await loaded.shutdown()

    try:
        asyncio.run(execute())
    except EphemeralDeliveryIncompleteError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE) from None
    except Exception as exc:
        typer.echo(f"Agent execution failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None


@manifest_app.command("validate")
def validate_manifest(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate one YAML Agent Manifest and print its Agent ID."""

    try:
        manifest = load_manifest(path)
    except Exception as exc:
        typer.echo(f"Manifest validation failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"valid: {manifest.metadata.id}")


@manifest_app.command("schema")
def print_manifest_schema() -> None:
    """Print the canonical Agent Manifest JSON Schema."""

    typer.echo(json.dumps(manifest_json_schema(), indent=2, ensure_ascii=False))


def _load_application(reference: str) -> KitsuneApp:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise typer.BadParameter("application must use module:attribute form")
    loaded = getattr(importlib.import_module(module_name), attribute)
    if not isinstance(loaded, KitsuneApp):
        raise typer.BadParameter(f"{reference} is not a KitsuneApp")
    return loaded


def main() -> None:
    """Run the unified Kitsune Typer application."""

    app()
