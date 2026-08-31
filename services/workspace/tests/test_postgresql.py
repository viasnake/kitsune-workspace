"""Live PostgreSQL portability gate for migrations and core control-plane writes."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import ManifestFactory, settings_for
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from kitsune_workspace.control_plane import ControlPlane
from kitsune_workspace.database import Database, InstanceLock
from kitsune_workspace.models import AgentDefinition, Run, WorkspaceLock
from kitsune_workspace.util import utcnow


@pytest.mark.postgresql
def test_live_postgresql_migration_registry_run_and_lock(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_url = os.getenv("KITSUNE_TEST_POSTGRES_URL")
    if not configured_url:
        pytest.skip("KITSUNE_TEST_POSTGRES_URL is not configured")
    root_url = make_url(configured_url)
    if root_url.get_backend_name() != "postgresql":
        pytest.fail("KITSUNE_TEST_POSTGRES_URL must use PostgreSQL")

    schema = f"kitsune_test_{uuid.uuid4().hex}"
    administrator = create_engine(root_url, pool_pre_ping=True)
    with administrator.begin() as connection:
        connection.execute(CreateSchema(schema))
    existing_options = str(root_url.query.get("options", "")).strip()
    search_path_option = f"-csearch_path={schema}"
    options = f"{existing_options} {search_path_option}".strip()
    scoped_url = root_url.update_query_dict({"options": options})
    scoped_url_text = scoped_url.render_as_string(hide_password=False)
    database: Database | None = None
    control: ControlPlane | None = None
    try:
        monkeypatch.setenv("KITSUNE_WORKSPACE__DATABASE_URL", scoped_url_text)
        service_root = Path(__file__).resolve().parents[1]
        command.upgrade(Config(service_root / "alembic.ini"), "head")
        database = Database(scoped_url_text)
        assert "agent_definitions" in inspect(database.engine).get_table_names()

        manifest_factory()
        settings = settings_for(tmp_path)
        settings.workspace.database_url = scoped_url_text
        control = ControlPlane(database, settings)
        report = control.registry.reload()
        assert report["added"] == ["demo-agent"]
        run, created = control.runs.create(
            agent_id="demo-agent",
            handler="default",
            source="on_demand",
            input_value={"database": "postgresql"},
        )
        assert created
        with database.session() as session:
            assert session.get(AgentDefinition, "demo-agent") is not None
            stored = session.get(Run, run.run_id)
            assert stored is not None and stored.input == {"database": "postgresql"}

        first = InstanceLock(database, "workspace:postgresql-test", 30)
        second = InstanceLock(database, "workspace:postgresql-test", 30)
        first.acquire()
        with pytest.raises(RuntimeError, match="held"):
            second.acquire()
        first.release()

        contenders = [InstanceLock(database, "workspace:postgresql-takeover", 30) for _ in range(2)]
        barrier = threading.Barrier(2)

        def acquire(lock: InstanceLock) -> bool:
            barrier.wait()
            try:
                lock.acquire()
            except RuntimeError:
                return False
            return True

        with database.session() as session:
            session.add(
                WorkspaceLock(
                    lock_name="workspace:postgresql-takeover",
                    owner_id="expired-owner",
                    acquired_at=utcnow() - timedelta(minutes=2),
                    expires_at=utcnow() - timedelta(minutes=1),
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(acquire, contenders))
        assert outcomes.count(True) == 1

        stale = InstanceLock(database, "workspace:postgresql-stale", 30)
        current = InstanceLock(database, "workspace:postgresql-stale", 30)
        stale.acquire()
        with database.session(fence=False) as session:
            lock = session.get(WorkspaceLock, stale.name)
            assert lock is not None
            lock.expires_at = utcnow() - timedelta(seconds=1)
        current.acquire()
        with pytest.raises(RuntimeError, match="ownership was lost"):
            stale.renew()
        stale.acquired = True
        stale.release()
        with database.session() as session:
            lock = session.get(WorkspaceLock, stale.name)
            assert lock is not None and lock.owner_id == current.owner_id
        current.release()
    finally:
        if control is not None:
            control.telemetry.shutdown()
        if database is not None:
            database.dispose()
        with administrator.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        administrator.dispose()
