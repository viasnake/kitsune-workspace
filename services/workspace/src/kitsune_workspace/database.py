"""SQLAlchemy engine, session, and Workspace instance-lock support."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine, create_engine, event, inspect, select, text, update
from sqlalchemy.engine import CursorResult, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

from .models import Base, WorkspaceLock
from .util import ensure_aware

CURRENT_SCHEMA_REVISION = "0001_workspace_schema"
_SESSION_MUTATED = "kitsune_session_mutated"


@event.listens_for(Session, "before_flush")
def _track_orm_flush(session: Session, *_: object) -> None:
    """Remember writes even when an autoflush clears Session.new/dirty before exit."""

    if session.new or session.dirty or session.deleted:
        session.info[_SESSION_MUTATED] = True


@event.listens_for(Session, "do_orm_execute")
def _track_session_dml(execute_state: ORMExecuteState) -> None:
    """Remember explicit UPDATE/DELETE/INSERT statements executed through Session."""

    statement = getattr(execute_state, "statement", None)
    if bool(getattr(statement, "is_dml", False)):
        execute_state.session.info[_SESSION_MUTATED] = True


def _require_private_directory(path: Path) -> None:
    """Create only the final private directory or validate it when it already exists."""

    created = False
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            parent_metadata = path.parent.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "SQLite storage directory parent must already exist; "
                f"create a dedicated ancestor before using: {path.parent}"
            ) from exc
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
            raise RuntimeError(
                f"SQLite storage directory parent must be a non-symlink directory: {path.parent}"
            ) from None
        if hasattr(os, "geteuid") and parent_metadata.st_uid != os.geteuid():
            raise RuntimeError(
                f"SQLite storage directory parent is not owned by this process: {path.parent}"
            ) from None
        if os.name == "posix":
            parent_mode = stat.S_IMODE(parent_metadata.st_mode)
            if parent_mode & 0o700 != 0o700 or parent_mode & 0o022:
                raise RuntimeError(
                    "SQLite storage directory parent must be owner-accessible and not "
                    f"group/world-writable: {path.parent}"
                ) from None
        try:
            path.mkdir(exist_ok=False, mode=0o700)
            created = True
        except FileNotFoundError as exc:
            raise RuntimeError(
                "SQLite storage directory parent must remain available during creation: "
                f"{path.parent}"
            ) from exc
        except FileExistsError:
            pass
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"SQLite storage directory must be a non-symlink directory: {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise RuntimeError(f"SQLite storage directory is not owned by this process: {path}")
    if os.name == "posix":
        mode = stat.S_IMODE(metadata.st_mode)
        if created:
            path.chmod(0o700)
            mode = stat.S_IMODE(path.lstat().st_mode)
        if mode & 0o700 != 0o700 or mode & 0o022:
            raise RuntimeError(
                "SQLite storage directory must be owner-accessible and not group/world-writable; "
                f"use a dedicated private directory: {path}"
            )


def _secure_regular_file(path: Path, *, create: bool) -> None:
    if path.is_symlink():
        raise RuntimeError(f"SQLite storage file must not be a symlink: {path}")
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"SQLite storage file must be a regular file: {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise RuntimeError(f"SQLite storage file is not owned by this process: {path}")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise RuntimeError(f"SQLite storage file is not private: {path}")
    finally:
        os.close(descriptor)


def _sqlite_path(url: str) -> Path | None:
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite" or database in {None, "", ":memory:"}:
        return None
    if parsed.query.get("mode") == "memory":
        return None
    assert database is not None
    return Path(database)


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class Database:
    """Own the SQLAlchemy engine and short-lived session factory."""

    def __init__(self, url: str) -> None:
        is_sqlite = make_url(url).get_backend_name() == "sqlite"
        self.sqlite_path = _sqlite_path(url)
        if self.sqlite_path is not None:
            self._secure_sqlite_storage(create_database=True)
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        if is_sqlite:
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.active_instance_lock: InstanceLock | None = None

    def _configure_sqlite(self, dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()
        self._secure_sqlite_storage(create_database=True)

    def _secure_sqlite_storage(self, *, create_database: bool) -> None:
        if self.sqlite_path is None:
            return
        _require_private_directory(self.sqlite_path.parent)
        _secure_regular_file(self.sqlite_path, create=create_database)
        _secure_regular_file(Path(f"{self.sqlite_path}-wal"), create=False)
        _secure_regular_file(Path(f"{self.sqlite_path}-shm"), create=False)

    def create_schema(self) -> None:
        """Bootstrap SQLite, but require Alembic-managed production databases.

        SQLite is the local developer default and remains self-bootstrapping. PostgreSQL must
        already be at the current migration revision so application startup cannot silently
        bypass an operator migration.
        """

        if self.engine.dialect.name == "sqlite":
            Base.metadata.create_all(self.engine)
            self._secure_sqlite_storage(create_database=True)
            return
        self.require_migrated_schema()

    def require_migrated_schema(self) -> None:
        """Fail unless Alembic recorded the current revision and all tables exist."""

        tables = set(inspect(self.engine).get_table_names())
        if "alembic_version" not in tables:
            raise RuntimeError(
                "database schema is not migrated; run "
                "`kitsune workspace migrate --config <path>` before Workspace"
            )
        with self.engine.connect() as connection:
            revisions = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        if revisions != {CURRENT_SCHEMA_REVISION}:
            actual = ", ".join(sorted(revisions)) or "none"
            raise RuntimeError(
                f"database schema revision is {actual}, expected {CURRENT_SCHEMA_REVISION}; "
                "run `kitsune workspace migrate --config <path>`"
            )
        missing = sorted(set(Base.metadata.tables) - tables)
        if missing:
            raise RuntimeError(
                "database migration is incomplete; missing tables: " + ", ".join(missing)
            )

    @contextlib.contextmanager
    def session(self, *, fence: bool = True) -> Iterator[Session]:
        """Yield a transaction-scoped session and roll back failures."""

        database_session = self.sessions()
        database_session.info[_SESSION_MUTATED] = False
        try:
            yield database_session
            mutated = bool(
                database_session.info.get(_SESSION_MUTATED)
                or database_session.new
                or database_session.dirty
                or database_session.deleted
            )
            authority = self.active_instance_lock
            if fence and mutated and authority is not None:
                database_session.flush()
                authority.fence(database_session, writes_started=True)
            database_session.commit()
        except BaseException:
            database_session.rollback()
            raise
        finally:
            database_session.close()

    def dispose(self) -> None:
        """Release engine resources."""

        self.engine.dispose()


class InstanceLock:
    """Database-backed lease enforcing a single active Workspace control plane."""

    def __init__(self, database: Database, name: str, ttl_seconds: int) -> None:
        self.database = database
        self.name = name
        self.ttl = timedelta(seconds=ttl_seconds)
        self.owner_id = str(uuid.uuid4())
        self.acquired = False
        self.confirmed_expires_at: datetime | None = None

    @property
    def locally_valid(self) -> bool:
        """Return whether the last committed ownership proof is still unexpired."""

        return bool(
            self.acquired
            and self.confirmed_expires_at is not None
            and ensure_aware(self.confirmed_expires_at) > utcnow()
        )

    def acquire(self) -> None:
        """Acquire the lease or fail if a live owner already holds it."""

        now = utcnow()
        with self.database.session(fence=False) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            query = select(WorkspaceLock).where(WorkspaceLock.lock_name == self.name)
            if session.get_bind().dialect.name == "postgresql":
                query = query.with_for_update()
            lock = session.scalar(query)
            if lock is None:
                session.add(
                    WorkspaceLock(
                        lock_name=self.name,
                        owner_id=self.owner_id,
                        acquired_at=now,
                        expires_at=now + self.ttl,
                    )
                )
                try:
                    session.flush()
                except IntegrityError as exc:
                    raise RuntimeError("another Workspace acquired the instance lock") from exc
            elif ensure_aware(lock.expires_at) > now and lock.owner_id != self.owner_id:
                expires = lock.expires_at.isoformat()
                raise RuntimeError(
                    f"Workspace instance lock is held by {lock.owner_id} until {expires}"
                )
            else:
                lock.owner_id = self.owner_id
                lock.acquired_at = now
                lock.expires_at = now + self.ttl
        self.acquired = True
        self.confirmed_expires_at = now + self.ttl
        self.database.active_instance_lock = self

    def renew(self) -> None:
        """Extend a held lease, detecting ownership loss."""

        if not self.acquired:
            return
        now = utcnow()
        with self.database.session(fence=False) as session:
            result = session.execute(
                update(WorkspaceLock)
                .where(
                    WorkspaceLock.lock_name == self.name,
                    WorkspaceLock.owner_id == self.owner_id,
                    WorkspaceLock.expires_at > now,
                )
                .values(expires_at=now + self.ttl)
            )
            if int(cast(CursorResult[Any], result).rowcount or 0) != 1:
                self._mark_local_authority_lost()
                raise RuntimeError("Workspace instance lock ownership was lost or expired")
        self.confirmed_expires_at = now + self.ttl

    def fence(self, session: Session, *, writes_started: bool = False) -> WorkspaceLock:
        """Lock and prove the unexpired owner row inside a mutation transaction."""

        if not self.locally_valid:
            self._mark_local_authority_lost()
            raise RuntimeError("Workspace instance lock ownership was lost or expired")
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            if session.in_transaction() and not writes_started:
                raise RuntimeError(
                    "SQLite instance lock fence must be the transaction's first read"
                )
            if not session.in_transaction():
                session.execute(text("BEGIN IMMEDIATE"))
        query = select(WorkspaceLock).where(WorkspaceLock.lock_name == self.name)
        if dialect == "postgresql":
            query = query.with_for_update()
        lock = session.scalar(query)
        now = utcnow()
        if lock is None or lock.owner_id != self.owner_id or ensure_aware(lock.expires_at) <= now:
            self._mark_local_authority_lost()
            raise RuntimeError("Workspace instance lock ownership was lost or expired")
        self.confirmed_expires_at = ensure_aware(lock.expires_at)
        return lock

    def _mark_local_authority_lost(self) -> None:
        """Retain a fail-closed sentinel after this process loses write authority."""

        self.acquired = False
        self.confirmed_expires_at = None

    def _unregister_local_authority(self) -> None:
        """Remove this InstanceLock from the Database during deliberate teardown."""

        self._mark_local_authority_lost()
        if self.database.active_instance_lock is self:
            self.database.active_instance_lock = None

    def release(self) -> None:
        """Expire the lease when this process still owns it."""

        if self.acquired:
            with self.database.session(fence=False) as session:
                session.execute(
                    update(WorkspaceLock)
                    .where(
                        WorkspaceLock.lock_name == self.name,
                        WorkspaceLock.owner_id == self.owner_id,
                    )
                    .values(expires_at=utcnow())
                )
        self._unregister_local_authority()
