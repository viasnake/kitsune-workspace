"""SQLite-backed at-least-once event outbox."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import UUID

from kitsune_contracts import EventSeverity, KitsuneEvent

from .settings import DEFAULT_OUTBOX_MAX_BYTES

EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE = 75


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
    """Open without following a final symlink, validate ownership, and force mode 0600."""

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


class OutboxFullError(RuntimeError):
    """Raised when a new event would exceed the configured durable queue limit."""

    def __init__(self, capacity: int, *, maximum_bytes: int | None = None) -> None:
        self.capacity = capacity
        self.maximum_bytes = maximum_bytes
        boundary = (
            f"{maximum_bytes} serialized bytes"
            if maximum_bytes is not None
            else f"{capacity} events"
        )
        super().__init__(f"Kitsune event outbox reached its capacity of {boundary}")


class EphemeralDeliveryIncompleteError(RuntimeError):
    """Raised when a managed ephemeral process exits with durable events still queued."""

    def __init__(self, *, remaining: int, failures: int) -> None:
        self.remaining = remaining
        self.failures = failures
        super().__init__(
            f"ephemeral event delivery incomplete: {remaining} queued events remain "
            f"after {failures} failed attempts"
        )


class NonRetryableEventDeliveryError(RuntimeError):
    """A Workspace response proving that at least one Event in a batch is poison."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail[:1000]
        super().__init__(f"Workspace rejected event batch with HTTP {status_code}: {self.detail}")


class RetryableEventDeliveryError(RuntimeError):
    """A retryable Workspace response with a server-requested minimum delay."""

    def __init__(self, *, status_code: int, detail: str, retry_after_seconds: int) -> None:
        self.status_code = status_code
        self.detail = detail[:1000]
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Workspace deferred event batch with HTTP {status_code}: {self.detail}")


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """One due event loaded from the durable outbox."""

    event: KitsuneEvent
    attempts: int


@dataclass(frozen=True, slots=True)
class OutboxDrainResult:
    """Bounded shutdown-drain result for operational reporting."""

    delivered: int
    remaining: int
    failures: int


class EventOutboxReservation:
    """Capacity held for a standard Event before its producing work begins."""

    def __init__(self, outbox: EventOutbox, slots: int) -> None:
        self.outbox = outbox
        self.remaining = slots
        self.released = False

    async def enqueue(self, event: KitsuneEvent) -> bool:
        """Persist one Event using a held slot."""

        return await self.outbox.enqueue_reserved(self, event)

    def release(self) -> None:
        """Return every unused slot to the Outbox."""

        self.outbox.release_reservation(self)


class EventOutbox:
    """Durable SQLite queue deduplicated by immutable Event ID."""

    def __init__(
        self,
        path: str | Path,
        *,
        capacity: int = 10_000,
        max_bytes: int = DEFAULT_OUTBOX_MAX_BYTES,
        max_event_bytes: int = 1_114_112,
    ) -> None:
        if capacity <= 0 or max_bytes <= 0 or max_event_bytes <= 0:
            raise ValueError("Outbox limits must be positive")
        if max_event_bytes > max_bytes:
            raise ValueError("Outbox per-event bytes cannot exceed total bytes")
        self.path = Path(path)
        self.capacity = capacity
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes
        self._capacity_lock = Lock()
        self._reserved_slots = 0
        self._reserved_bytes = 0
        self._secure_storage(create_database=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self._secure_storage(create_database=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        self._secure_storage(create_database=True)
        return connection

    def _secure_storage(self, *, create_database: bool) -> None:
        _require_private_directory(self.path.parent)
        _secure_regular_file(self.path, create=create_database)
        _secure_regular_file(Path(f"{self.path}-wal"), create=False)
        _secure_regular_file(Path(f"{self.path}-shm"), create=False)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_event_outbox_due "
                "ON event_outbox(next_attempt_at, created_at)"
            )

    async def enqueue(self, event: KitsuneEvent) -> bool:
        """Persist ``event`` unless its Event ID already exists."""

        return await asyncio.to_thread(self._enqueue_unreserved, event)

    async def reserve(self, slots: int) -> EventOutboxReservation:
        """Hold capacity so required Events cannot be displaced by later optional Events."""

        if slots <= 0:
            raise ValueError("reserved outbox slots must be positive")
        return await asyncio.to_thread(self._reserve, slots)

    def _reserve(self, slots: int) -> EventOutboxReservation:
        with self._capacity_lock, self._connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0])
            if count + self._reserved_slots + slots > self.capacity:
                raise OutboxFullError(self.capacity)
            retained_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(payload_bytes), 0) FROM event_outbox"
                ).fetchone()[0]
            )
            requested_bytes = slots * self.max_event_bytes
            if retained_bytes + self._reserved_bytes + requested_bytes > self.max_bytes:
                raise OutboxFullError(self.capacity, maximum_bytes=self.max_bytes)
            self._reserved_slots += slots
            self._reserved_bytes += requested_bytes
            return EventOutboxReservation(self, slots)

    def _enqueue_unreserved(self, event: KitsuneEvent) -> bool:
        with self._capacity_lock:
            return self._insert(
                event,
                capacity=self.capacity - self._reserved_slots,
                byte_capacity=self.max_bytes - self._reserved_bytes,
            )

    async def enqueue_reserved(
        self,
        reservation: EventOutboxReservation,
        event: KitsuneEvent,
    ) -> bool:
        """Persist one Event against capacity previously held by ``reservation``."""

        return await asyncio.to_thread(self._enqueue_reserved_sync, reservation, event)

    def _enqueue_reserved_sync(
        self,
        reservation: EventOutboxReservation,
        event: KitsuneEvent,
    ) -> bool:
        with self._capacity_lock:
            if reservation.outbox is not self or reservation.released:
                raise RuntimeError("outbox reservation is not active")
            if reservation.remaining <= 0:
                raise RuntimeError("outbox reservation has no remaining capacity")
            inserted = self._insert(
                event,
                capacity=self.capacity - self._reserved_slots + 1,
                byte_capacity=self.max_bytes - self._reserved_bytes + self.max_event_bytes,
            )
            if inserted:
                reservation.remaining -= 1
                self._reserved_slots -= 1
                self._reserved_bytes -= self.max_event_bytes
            return inserted

    def release_reservation(self, reservation: EventOutboxReservation) -> None:
        """Release the unused capacity held by ``reservation`` exactly once."""

        with self._capacity_lock:
            if reservation.outbox is not self or reservation.released:
                return
            self._reserved_slots -= reservation.remaining
            self._reserved_bytes -= reservation.remaining * self.max_event_bytes
            reservation.remaining = 0
            reservation.released = True

    def _insert(
        self,
        event: KitsuneEvent,
        *,
        capacity: int,
        byte_capacity: int | None = None,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        payload = event.model_dump_json()
        payload_bytes = len(payload.encode())
        available_bytes = self.max_bytes if byte_capacity is None else byte_capacity
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM event_outbox WHERE event_id = ?", (str(event.event_id),)
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                return False
            if payload_bytes > self.max_event_bytes:
                connection.execute("ROLLBACK")
                raise OutboxFullError(self.capacity, maximum_bytes=self.max_event_bytes)
            count = int(connection.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0])
            if count >= capacity:
                connection.execute("ROLLBACK")
                raise OutboxFullError(self.capacity)
            retained_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(payload_bytes), 0) FROM event_outbox"
                ).fetchone()[0]
            )
            if retained_bytes + payload_bytes > available_bytes:
                connection.execute("ROLLBACK")
                raise OutboxFullError(self.capacity, maximum_bytes=self.max_bytes)
            connection.execute(
                "INSERT INTO event_outbox"
                "(event_id, payload, payload_bytes, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(event.event_id), payload, payload_bytes, now, now),
            )
            connection.execute("COMMIT")
            return True

    async def pending(
        self, *, limit: int = 100, max_batch_bytes: int | None = None
    ) -> list[PendingEvent]:
        """Return due events in stable insertion order."""

        return await asyncio.to_thread(
            self._pending,
            limit,
            max_batch_bytes or self.max_event_bytes,
        )

    def _pending(self, limit: int, max_batch_bytes: int) -> list[PendingEvent]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            headers = connection.execute(
                "SELECT event_id, payload_bytes FROM event_outbox "
                "WHERE next_attempt_at <= ? ORDER BY created_at, event_id LIMIT ?",
                (now, limit),
            ).fetchall()
            event_ids: list[str] = []
            selected_bytes = 0
            for row in headers:
                payload_bytes = int(row["payload_bytes"])
                if event_ids and selected_bytes + payload_bytes > max_batch_bytes:
                    break
                event_ids.append(str(row["event_id"]))
                selected_bytes += payload_bytes
            if not event_ids:
                return []
            placeholders = ",".join("?" for _ in event_ids)
            rows = connection.execute(
                "SELECT payload, attempts FROM event_outbox "
                f"WHERE event_id IN ({placeholders}) ORDER BY created_at, event_id",
                event_ids,
            ).fetchall()
        return [
            PendingEvent(
                event=KitsuneEvent.model_validate_json(row["payload"]), attempts=row["attempts"]
            )
            for row in rows
        ]

    async def mark_delivered(self, event_ids: Sequence[UUID]) -> None:
        """Atomically delete events acknowledged by Workspace."""

        if event_ids:
            await asyncio.to_thread(self._mark_delivered, event_ids)

    def _mark_delivered(self, event_ids: Sequence[UUID]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM event_outbox WHERE event_id = ?",
                [(str(event_id),) for event_id in event_ids],
            )

    async def mark_failed(
        self,
        event_ids: Sequence[UUID],
        *,
        error: str,
        initial_backoff: float,
        maximum_backoff: float,
        minimum_delay: float = 0,
    ) -> None:
        """Record a failed attempt and schedule bounded exponential retry."""

        if event_ids:
            await asyncio.to_thread(
                self._mark_failed,
                event_ids,
                error,
                initial_backoff,
                maximum_backoff,
                minimum_delay,
            )

    def _mark_failed(
        self,
        event_ids: Sequence[UUID],
        error: str,
        initial_backoff: float,
        maximum_backoff: float,
        minimum_delay: float,
    ) -> None:
        with self._connect() as connection:
            for event_id in event_ids:
                row = connection.execute(
                    "SELECT attempts FROM event_outbox WHERE event_id = ?", (str(event_id),)
                ).fetchone()
                if row is None:
                    continue
                attempts = int(row["attempts"]) + 1
                exponent = attempts - 1
                if maximum_backoff <= initial_backoff:
                    delay = maximum_backoff
                else:
                    saturation_exponent = math.ceil(
                        math.log2(maximum_backoff) - math.log2(initial_backoff)
                    )
                    delay = (
                        maximum_backoff
                        if exponent >= saturation_exponent
                        else min(math.ldexp(initial_backoff, exponent), maximum_backoff)
                    )
                delay = min(maximum_backoff, max(delay, minimum_delay))
                next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                connection.execute(
                    "UPDATE event_outbox SET attempts = ?, next_attempt_at = ?, last_error = ? "
                    "WHERE event_id = ?",
                    (attempts, next_attempt, error[:1000], str(event_id)),
                )

    async def size(self) -> int:
        """Return the number of currently queued unique events."""

        return await asyncio.to_thread(self._size)

    def _size(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0])

    async def seconds_until_next_attempt(self) -> float | None:
        """Return the time until the earliest queued retry becomes due."""

        return await asyncio.to_thread(self._seconds_until_next_attempt)

    def _seconds_until_next_attempt(self) -> float | None:
        with self._connect() as connection:
            row = connection.execute("SELECT MIN(next_attempt_at) FROM event_outbox").fetchone()
        if row is None or row[0] is None:
            return None
        due_at = datetime.fromisoformat(str(row[0]))
        return max(0.0, (due_at - datetime.now(UTC)).total_seconds())

    async def flush_once(
        self,
        sender: Callable[[Sequence[KitsuneEvent]], Awaitable[None]],
        *,
        batch_size: int,
        max_batch_bytes: int | None = None,
        initial_backoff: float,
        maximum_backoff: float,
    ) -> int:
        """Attempt one due batch and return the number delivered."""

        pending = await self.pending(
            limit=batch_size,
            max_batch_bytes=max_batch_bytes or self.max_event_bytes,
        )
        if not pending:
            return 0
        events = [item.event for item in pending]
        try:
            await sender(events)
        except NonRetryableEventDeliveryError:
            return await self._isolate_rejected_events(
                sender,
                events,
                initial_backoff=initial_backoff,
                maximum_backoff=maximum_backoff,
            )
        except Exception as exc:
            await self.mark_failed(
                [event.event_id for event in events],
                error=f"{type(exc).__name__}: {exc}",
                initial_backoff=initial_backoff,
                maximum_backoff=maximum_backoff,
                minimum_delay=float(getattr(exc, "retry_after_seconds", 0)),
            )
            raise
        await self.mark_delivered([event.event_id for event in events])
        return len(events)

    async def _isolate_rejected_events(
        self,
        sender: Callable[[Sequence[KitsuneEvent]], Awaitable[None]],
        events: Sequence[KitsuneEvent],
        *,
        initial_backoff: float,
        maximum_backoff: float,
    ) -> int:
        """Bisect a rejected batch and quarantine only irreducible poison Events."""

        try:
            await sender(events)
        except NonRetryableEventDeliveryError as exc:
            if len(events) == 1:
                event = events[0]
                replaced = await asyncio.to_thread(self._replace_with_rejection, event, exc)
                return 0 if replaced else 1
            midpoint = len(events) // 2
            first = await self._isolate_rejected_events(
                sender,
                events[:midpoint],
                initial_backoff=initial_backoff,
                maximum_backoff=maximum_backoff,
            )
            second = await self._isolate_rejected_events(
                sender,
                events[midpoint:],
                initial_backoff=initial_backoff,
                maximum_backoff=maximum_backoff,
            )
            return first + second
        except Exception as exc:
            await self.mark_failed(
                [event.event_id for event in events],
                error=f"{type(exc).__name__}: {exc}",
                initial_backoff=initial_backoff,
                maximum_backoff=maximum_backoff,
                minimum_delay=float(getattr(exc, "retry_after_seconds", 0)),
            )
            raise
        await self.mark_delivered([event.event_id for event in events])
        return len(events)

    def _replace_with_rejection(
        self,
        event: KitsuneEvent,
        error: NonRetryableEventDeliveryError,
    ) -> bool:
        rejection = None
        if event.type != "kitsune.event.rejected":
            rejection = KitsuneEvent(
                type="kitsune.event.rejected",
                occurred_at=datetime.now(UTC),
                agent_id=event.agent_id,
                runtime_instance_id=event.runtime_instance_id,
                run_id=event.run_id,
                parent_run_id=event.parent_run_id,
                correlation_id=event.correlation_id,
                trace_id=event.trace_id,
                severity=EventSeverity.ERROR,
                payload={
                    "rejected_event_id": str(event.event_id),
                    "rejected_event_type": event.type,
                    "reason": "workspace_rejected",
                    "status_code": error.status_code,
                },
            )
        now = datetime.now(UTC).isoformat()
        serialized_rejection = rejection.model_dump_json() if rejection is not None else None
        rejection_bytes = (
            len(serialized_rejection.encode()) if serialized_rejection is not None else 0
        )
        with self._capacity_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM event_outbox WHERE event_id = ?",
                (str(event.event_id),),
            )
            retained_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(payload_bytes), 0) FROM event_outbox"
                ).fetchone()[0]
            )
            rejection_fits = (
                rejection is not None
                and rejection_bytes <= self.max_event_bytes
                and retained_bytes + self._reserved_bytes + rejection_bytes <= self.max_bytes
            )
            if rejection_fits and rejection is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO event_outbox"
                    "(event_id, payload, payload_bytes, created_at, next_attempt_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(rejection.event_id),
                        serialized_rejection,
                        rejection_bytes,
                        now,
                        now,
                    ),
                )
            connection.execute("COMMIT")
        return rejection_fits

    async def drain(
        self,
        sender: Callable[[Sequence[KitsuneEvent]], Awaitable[None]],
        *,
        batch_size: int,
        initial_backoff: float,
        maximum_backoff: float,
        deadline_monotonic: float,
        max_batch_bytes: int | None = None,
    ) -> OutboxDrainResult:
        """Retry all queued events until empty or a monotonic deadline."""

        delivered = 0
        failures = 0
        while monotonic() < deadline_monotonic:
            remaining = await self.size()
            if remaining == 0:
                return OutboxDrainResult(delivered=delivered, remaining=0, failures=failures)
            delay = await self.seconds_until_next_attempt()
            time_left = deadline_monotonic - monotonic()
            if time_left <= 0:
                break
            if delay is not None and delay > 0:
                await asyncio.sleep(min(delay, time_left))
                continue
            try:
                delivered += await self.flush_once(
                    sender,
                    batch_size=batch_size,
                    max_batch_bytes=max_batch_bytes,
                    initial_backoff=initial_backoff,
                    maximum_backoff=maximum_backoff,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
        return OutboxDrainResult(
            delivered=delivered, remaining=await self.size(), failures=failures
        )
