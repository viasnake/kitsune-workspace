"""Typer operator CLI for serving and calling Kitsune Workspace."""

from __future__ import annotations

import json
import os
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn
from alembic import command
from alembic.config import Config

from .app import create_app
from .config import WorkspaceSettings
from .database import Database

workspace_app = typer.Typer(no_args_is_help=True, help="Serve and operate Kitsune Workspace.")
agents_app = typer.Typer(no_args_is_help=True, help="Inspect and control Agent Definitions.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect and cancel Runs.")
schedules_app = typer.Typer(no_args_is_help=True, help="Inspect schedules.")
tokens_app = typer.Typer(no_args_is_help=True, help="Issue and revoke Agent credentials.")
workspace_app.add_typer(agents_app, name="agents")
workspace_app.add_typer(runs_app, name="runs")
workspace_app.add_typer(schedules_app, name="schedules")
workspace_app.add_typer(tokens_app, name="tokens")
app = workspace_app


def _base_url(value: str | None) -> str:
    return (value or os.getenv("KITSUNE_WORKSPACE_URL") or "http://127.0.0.1:8080").rstrip("/")


def _headers(mutating: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    cookie = os.getenv("KITSUNE_WORKSPACE_SESSION")
    if cookie:
        headers["Cookie"] = cookie
    if mutating:
        csrf = os.getenv("KITSUNE_CSRF_TOKEN")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


def _request(method: str, path: str, base_url: str | None, body: Any = None) -> Any:
    response = httpx.request(
        method,
        f"{_base_url(base_url)}{path}",
        json=body,
        headers=_headers(method != "GET"),
        timeout=30,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        typer.echo(response.text, err=True)
        raise typer.Exit(1) from exc
    if response.status_code == 204:
        return None
    return response.json()


def _display(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _migration_resources() -> Any:
    """Resolve packaged migrations or the canonical editable-source directory."""

    packaged = files("kitsune_workspace").joinpath("migrations")
    if packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[2] / "migrations"
    if not source.is_dir():
        raise RuntimeError("Workspace migration resources are unavailable")
    return source


@workspace_app.command("serve")
def serve(
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Workspace TOML configuration file.",
    ),
) -> None:
    """Start the Workspace API and scheduler, serving configured static assets when present."""

    settings = WorkspaceSettings.load(config)
    uvicorn.run(
        create_app(settings),
        host=settings.workspace.bind_host,
        port=settings.workspace.bind_port,
        proxy_headers=False,
    )


@workspace_app.command("migrate")
def migrate(
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Workspace TOML configuration file.",
    ),
) -> None:
    """Upgrade the configured database to the packaged Workspace schema revision."""

    settings = WorkspaceSettings.load(config)
    storage = Database(settings.workspace.database_url)
    storage.dispose()
    with as_file(_migration_resources()) as migration_directory:
        alembic_config = Config()
        alembic_config.set_main_option("script_location", str(migration_directory))
        alembic_config.set_main_option(
            "sqlalchemy.url", settings.workspace.database_url.replace("%", "%%")
        )
        alembic_config.attributes["workspace_settings"] = settings
        command.upgrade(alembic_config, "head")
    typer.echo("Workspace database is at revision 0001_workspace_schema.")


@workspace_app.command("reload")
def reload_definitions(
    url: str | None = typer.Option(None, help="Workspace base URL."),
) -> None:
    """Atomically reload every Agent Manifest through the admin API."""

    _display(_request("POST", "/api/admin/reload", url))


@agents_app.command("list")
def agents_list(url: str | None = typer.Option(None)) -> None:
    """List active Agent Definitions."""

    _display(_request("GET", "/api/agents", url))


@agents_app.command("show")
def agents_show(agent_id: str, url: str | None = typer.Option(None)) -> None:
    """Show one Agent Definition and reported runtime state."""

    _display(_request("GET", f"/api/agents/{agent_id}", url))


def _agent_action(agent_id: str, action: str, url: str | None) -> None:
    _display(_request("POST", f"/api/agents/{agent_id}/{action}", url))


@agents_app.command("start")
def agents_start(agent_id: str, url: str | None = typer.Option(None)) -> None:
    """Start a manifest-defined resident Agent."""

    _agent_action(agent_id, "start", url)


@agents_app.command("stop")
def agents_stop(agent_id: str, url: str | None = typer.Option(None)) -> None:
    """Stop Workspace-owned resident Runtime Instances for an Agent."""

    _agent_action(agent_id, "stop", url)


@agents_app.command("restart")
def agents_restart(agent_id: str, url: str | None = typer.Option(None)) -> None:
    """Restart a resident Agent into a fresh Runtime Instance."""

    _agent_action(agent_id, "restart", url)


@runs_app.command("list")
def runs_list(
    agent_id: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    url: str | None = typer.Option(None),
) -> None:
    """List persisted Runs."""

    query = str(
        httpx.QueryParams(
            {key: value for key, value in {"agent_id": agent_id, "status": status}.items() if value}
        )
    )
    _display(_request("GET", f"/api/runs{f'?{query}' if query else ''}", url))


@runs_app.command("show")
def runs_show(run_id: str, url: str | None = typer.Option(None)) -> None:
    """Show one Run, including usage and child IDs."""

    _display(_request("GET", f"/api/runs/{run_id}", url))


@runs_app.command("cancel")
def runs_cancel(run_id: str, url: str | None = typer.Option(None)) -> None:
    """Cancel one non-terminal Run."""

    _display(_request("POST", f"/api/runs/{run_id}/cancel", url))


@schedules_app.command("list")
def schedules_list(url: str | None = typer.Option(None)) -> None:
    """List persistent schedule cursors."""

    _display(_request("GET", "/api/schedules", url))


@tokens_app.command("issue")
def tokens_issue(
    agent_id: str,
    description: str | None = typer.Option(None),
    url: str | None = typer.Option(None),
) -> None:
    """Issue an Agent-scoped bearer token and print its one-time plaintext value."""

    _display(
        _request(
            "POST",
            "/api/admin/tokens",
            url,
            {"agent_id": agent_id, "description": description},
        )
    )


@tokens_app.command("revoke")
def tokens_revoke(credential_id: str, url: str | None = typer.Option(None)) -> None:
    """Revoke one Agent credential."""

    _display(_request("DELETE", f"/api/admin/tokens/{credential_id}", url))


if __name__ == "__main__":
    workspace_app()
