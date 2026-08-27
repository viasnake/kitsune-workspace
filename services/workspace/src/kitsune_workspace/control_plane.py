"""Active control-plane loops for scheduling, dispatch, timeout, health, and retention."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from .config import WorkspaceSettings
from .database import Database, InstanceLock
from .manifest import ManifestRegistry, next_fire_time
from .models import AgentDefinition, Run, RuntimeInstance, Schedule
from .runtime import RuntimeManager, RuntimeOperationError, _runtime_snapshot_hash
from .services import (
    EventService,
    QueueCapacityExceeded,
    RetentionService,
    RunService,
    RunServiceError,
    audit,
)
from .storage import StorageQuotaExceeded
from .telemetry import WorkspaceTelemetry
from .util import TERMINAL_RUN_STATUSES, ensure_aware, utcnow

logger = logging.getLogger("kitsune.workspace")


class EventBus:
    """Fan out bounded best-effort control-plane updates to SSE clients."""

    def __init__(
        self,
        queue_size: int = 500,
        max_subscribers: int = 100,
        max_per_principal: int = 5,
        max_per_ip: int = 10,
    ) -> None:
        self.queue_size = queue_size
        self.max_subscribers = max_subscribers
        self.max_per_principal = max_per_principal
        self.max_per_ip = max_per_ip
        self._subscribers: dict[asyncio.Queue[dict[str, Any]], tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish without letting a slow browser block control-plane work."""

        message = {"type": event_type, "data": data, "occurred_at": utcnow().isoformat()}
        async with self._lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    continue

    async def open_subscription(
        self, principal_key: str, remote_key: str
    ) -> asyncio.Queue[dict[str, Any]]:
        """Reserve one bounded subscriber slot before response headers are sent."""

        async with self._lock:
            if len(self._subscribers) >= self.max_subscribers:
                raise EventStreamLimitExceeded("global")
            principal_connections = sum(
                registered_principal == principal_key
                for registered_principal, _ in self._subscribers.values()
            )
            if principal_connections >= self.max_per_principal:
                raise EventStreamLimitExceeded("principal")
            remote_connections = sum(
                registered_remote == remote_key
                for _, registered_remote in self._subscribers.values()
            )
            if remote_connections >= self.max_per_ip:
                raise EventStreamLimitExceeded("ip")
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self.queue_size)
            self._subscribers[queue] = (principal_key, remote_key)
            return queue

    async def close_subscription(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Release a previously reserved subscriber slot."""

        async with self._lock:
            self._subscribers.pop(queue, None)

    async def messages(self, queue: asyncio.Queue[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Yield queued messages and periodic heartbeat comments."""

        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield {"type": "heartbeat", "data": {}, "occurred_at": utcnow().isoformat()}

    async def subscribe(
        self, principal_key: str = "internal", remote_key: str = "internal"
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield messages and periodic heartbeat comments for one SSE connection."""

        queue = await self.open_subscription(principal_key, remote_key)
        try:
            async for message in self.messages(queue):
                yield message
        finally:
            await self.close_subscription(queue)


class EventStreamLimitExceeded(RuntimeError):
    """Raised when an SSE connection would exceed a live subscriber boundary."""

    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        super().__init__(f"SSE {boundary} connection limit reached")


class ControlPlane:
    """One active scheduler and runtime-control process guarded by a database lease."""

    def __init__(self, database: Database, settings: WorkspaceSettings) -> None:
        self.database = database
        self.settings = settings
        self.events = EventBus(
            max_subscribers=settings.security.sse_max_connections,
            max_per_principal=settings.security.sse_max_connections_per_principal,
            max_per_ip=settings.security.sse_max_connections_per_ip,
        )
        self.registry = ManifestRegistry(database, settings)
        self.telemetry = WorkspaceTelemetry(settings, database)
        self.runtime = RuntimeManager(database, settings, self.events.publish, self.telemetry)
        self.runs = RunService(database, settings, self.runtime)
        self.events_service = EventService(database, settings, self.telemetry)
        self.runtime.set_outbox_recovery(self.events_service.recover_outbox)
        self.retention = RetentionService(database, settings)
        self.instance_lock = InstanceLock(
            database,
            f"workspace:{settings.workspace.name}",
            settings.scheduler.lock_ttl_seconds,
        )
        self._task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._reload = asyncio.Event()
        self._last_retention = utcnow()
        self._last_lock_renewal = utcnow()
        self.last_loop_at = None
        self.scheduler_error: str | None = None
        self.monitor_error: str | None = None
        self.lease_error: str | None = None
        self.lease_failed = False

    @property
    def accepting_operations(self) -> bool:
        """Return whether this process still owns authority to mutate control-plane state."""

        return not self.lease_failed and self.instance_lock.locally_valid

    @property
    def last_error(self) -> str | None:
        """Return any independently owned control-loop failure."""

        return self.lease_error or self.scheduler_error or self.monitor_error

    async def start(self) -> dict[str, Any]:
        """Acquire the active lock, load Manifests, reconcile, and start control loops."""

        self._stop.clear()
        self.lease_failed = False
        self.scheduler_error = None
        self.monitor_error = None
        self.lease_error = None
        await asyncio.to_thread(self.instance_lock.acquire)
        self._renew_task = asyncio.create_task(
            self._renew_loop(), name="kitsune-instance-lock-renewal"
        )
        try:
            report = await asyncio.to_thread(self.registry.reload)
            await self.reconcile()
        except BaseException:
            if self._renew_task is not None:
                self._renew_task.cancel()
                await asyncio.gather(self._renew_task, return_exceptions=True)
                self._renew_task = None
            await asyncio.to_thread(self.instance_lock.release)
            raise
        self._task = asyncio.create_task(self._loop(), name="kitsune-control-plane")
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="kitsune-runtime-monitor"
        )
        return report

    async def stop(self) -> None:
        """Stop loops and managed runtimes, then release the active lease."""

        self._stop.set()
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._task, self._monitor_task, self._renew_task)
            if task and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.instance_lock.locally_valid:
                try:
                    await self.runtime.shutdown()
                except RuntimeError:
                    await self.runtime.abandon()
            else:
                await self.runtime.abandon()
            if self.instance_lock.locally_valid:
                await asyncio.to_thread(self.instance_lock.release)
            else:
                self.instance_lock._unregister_local_authority()
        finally:
            self.telemetry.shutdown()

    def request_reload(self) -> None:
        """Ask the control loop to perform an atomic reload after SIGHUP."""

        self._reload.set()

    async def reload(self) -> dict[str, Any]:
        """Reload all Manifests atomically and reconcile desired runtime state."""

        if not self.accepting_operations:
            raise RuntimeError("control-plane instance lock is not owned")
        report = await asyncio.to_thread(self.registry.reload)
        await self.reconcile()
        await self.events.publish("reload", report)
        return report

    async def reconcile(self) -> None:
        """Apply resident desired state without changing External lifecycle ownership."""

        with self.database.session() as session:
            definitions = list(session.scalars(select(AgentDefinition)))
        for definition in definitions:
            if not self.accepting_operations:
                return
            if definition.runtime_mode != "resident" or definition.runtime_adapter == "external":
                continue
            if definition.active and definition.desired_state == "running":
                try:
                    with self.database.session() as session:
                        active = session.scalar(
                            select(RuntimeInstance)
                            .where(
                                RuntimeInstance.agent_id == definition.agent_id,
                                RuntimeInstance.status.in_(
                                    ["pending", "starting", "ready", "unhealthy"]
                                ),
                            )
                            .order_by(RuntimeInstance.started_at.desc())
                        )
                    expected_hash = _runtime_snapshot_hash(definition.snapshot)
                    if (
                        active is not None
                        and (active.runtime_metadata or {}).get("runtime_hash") != expected_hash
                    ):
                        await self.runtime.restart_agent(definition.agent_id)
                    else:
                        if (
                            active is not None
                            and (active.runtime_metadata or {}).get("manifest_hash")
                            != definition.content_hash
                        ):
                            with self.database.session() as session:
                                current = session.get(RuntimeInstance, active.runtime_instance_id)
                                if current is not None:
                                    metadata = dict(current.runtime_metadata or {})
                                    metadata["manifest_hash"] = definition.content_hash
                                    current.runtime_metadata = metadata
                        await self.runtime.start_agent(definition.agent_id)
                except (RuntimeOperationError, StorageQuotaExceeded) as exc:
                    logger.error(
                        json.dumps(
                            {
                                "event": "kitsune.runtime.reconcile_failed",
                                "agent_id": definition.agent_id,
                                "error_type": type(exc).__name__,
                                "message": "Runtime reconciliation failed",
                            }
                        )
                    )
            elif definition.desired_state == "stopped" or not definition.active:
                await self.runtime.stop_agent(definition.agent_id)

    async def _loop(self) -> None:
        interval = self.settings.scheduler.poll_interval_seconds
        while not self._stop.is_set():
            try:
                if not self.accepting_operations:
                    return
                self.last_loop_at = utcnow()
                if self._reload.is_set():
                    self._reload.clear()
                    try:
                        await self.reload()
                    except ValueError as exc:
                        logger.error(
                            json.dumps(
                                {
                                    "event": "kitsune.manifest.reload_rejected",
                                    "error_type": type(exc).__name__,
                                    "message": "Manifest reload rejected",
                                }
                            )
                        )
                await self.dispatch_schedules()
                if not self.accepting_operations:
                    return
                await self.runs.dispatch_available()
                if not self.accepting_operations:
                    return
                await self.runs.enforce_timeouts()
                if not self.accepting_operations:
                    return
                now = utcnow()
                if now - self._last_retention >= timedelta(
                    seconds=self.settings.scheduler.retention_interval_seconds
                ):
                    await asyncio.to_thread(self.retention.run, now)
                    self._last_retention = now
                self.scheduler_error = None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.scheduler_error = f"Control-plane loop failed ({type(exc).__name__})"
                logger.exception(
                    json.dumps(
                        {
                            "event": "kitsune.control_plane.loop_failed",
                            "error_type": type(exc).__name__,
                            "message": "Control-plane loop failed",
                        }
                    )
                )
            await asyncio.sleep(interval)

    async def _monitor_loop(self) -> None:
        """Observe Runtimes independently so slow probes cannot starve scheduler duties."""

        interval = self.settings.scheduler.poll_interval_seconds
        while not self._stop.is_set():
            try:
                if not self.accepting_operations:
                    return
                await self.runtime.monitor(respect_backoff=True)
                self.monitor_error = None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.monitor_error = f"Runtime monitor loop failed ({type(exc).__name__})"
                logger.exception(
                    json.dumps(
                        {
                            "event": "kitsune.runtime.monitor_failed",
                            "error_type": type(exc).__name__,
                            "message": "Runtime monitor loop failed",
                        }
                    )
                )
            await asyncio.sleep(interval)

    async def _renew_loop(self) -> None:
        """Renew independently from slow scheduling and fail closed on ownership loss."""

        interval = max(0.1, self.settings.scheduler.lock_ttl_seconds / 3)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                await asyncio.to_thread(self.instance_lock.renew)
                self._last_lock_renewal = utcnow()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.lease_failed = True
            self.lease_error = f"Instance lock renewal failed ({type(exc).__name__})"
            self._stop.set()
            if self._task is not None and not self._task.done():
                self._task.cancel()
            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
            logger.exception(
                json.dumps(
                    {
                        "event": "kitsune.instance_lock.renewal_failed",
                        "error_type": type(exc).__name__,
                        "message": "Instance lock renewal failed",
                    }
                )
            )
            await self.runtime.abandon()

    async def dispatch_schedules(self) -> int:
        """Apply cron misfire and overlap policies, then create due Runs."""

        now = utcnow()
        with self.database.session() as session:
            due_ids = list(
                session.scalars(
                    select(Schedule.id)
                    .where(Schedule.enabled.is_(True), Schedule.next_fire_at <= now)
                    .order_by(Schedule.next_fire_at)
                )
            )
        created = 0
        for schedule_id in due_ids:
            with self.database.session() as session:
                schedule = session.get(Schedule, schedule_id)
                if schedule is None or not schedule.enabled:
                    continue
                scheduled_for = ensure_aware(schedule.next_fire_at)
                active_ids = list(
                    session.scalars(
                        select(Run.run_id).where(
                            Run.agent_id == schedule.agent_id,
                            Run.trigger_id == schedule.trigger_id,
                            Run.status.not_in(TERMINAL_RUN_STATUSES),
                        )
                    )
                )
                schedule.next_fire_at = next_fire_time(
                    schedule.cron, schedule.timezone, scheduled_for
                )
                schedule.last_fire_at = scheduled_for
                snapshot = {
                    "agent_id": schedule.agent_id,
                    "trigger_id": schedule.trigger_id,
                    "handler": schedule.handler,
                    "overlap": schedule.overlap,
                    "misfire_grace_seconds": schedule.misfire_grace_seconds,
                    "scheduled_for": scheduled_for,
                    "active_ids": active_ids,
                }
            delay = (now - snapshot["scheduled_for"]).total_seconds()
            with self.telemetry.span(
                "kitsune.schedule.dispatch",
                agent_id=snapshot["agent_id"],
                trigger_id=snapshot["trigger_id"],
            ):
                self.telemetry.schedule_delay.record(
                    max(0.0, delay), {"agent_id": snapshot["agent_id"]}
                )
            if delay > snapshot["misfire_grace_seconds"]:
                outcome = "misfire_skipped"
            elif snapshot["active_ids"] and snapshot["overlap"] == "skip":
                outcome = "overlap_skipped"
            else:
                if snapshot["active_ids"] and snapshot["overlap"] == "replace":
                    for run_id in snapshot["active_ids"]:
                        with contextlib.suppress(RunServiceError, RuntimeOperationError):
                            await self.runs.cancel(run_id)
                try:
                    _, was_created = self.runs.create(
                        agent_id=snapshot["agent_id"],
                        handler=snapshot["handler"],
                        source="schedule",
                        trigger_id=snapshot["trigger_id"],
                        input_value={},
                        idempotency_key=(
                            f"schedule:{snapshot['trigger_id']}:{snapshot['scheduled_for'].isoformat()}"
                        ),
                    )
                    created += int(was_created)
                    outcome = "created" if was_created else "duplicate"
                except QueueCapacityExceeded:
                    outcome = "queue_full"
                except StorageQuotaExceeded:
                    outcome = "storage_quota"
            with self.database.session() as session:
                schedule = session.get(Schedule, schedule_id)
                if schedule:
                    schedule.last_outcome = outcome
                    audit(
                        session,
                        actor_type="scheduler",
                        actor_id=self.settings.workspace.name,
                        actor_role="scheduler",
                        action="schedule.dispatch",
                        resource_type="schedule",
                        resource_id=str(schedule_id),
                        outcome=outcome,
                        details={"scheduled_for": snapshot["scheduled_for"].isoformat()},
                        redacted_keys=self.settings.security.redacted_keys,
                    )
            await self.events.publish("schedule", {"schedule_id": schedule_id, "outcome": outcome})
        return created
