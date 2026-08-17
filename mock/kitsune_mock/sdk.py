from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .plugins import Plugin

EventSink = Callable[[dict[str, Any]], Awaitable[None]]
Handler = Callable[["RunContext", Any], Awaitable[Any]]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunContext:
    agent_id: str
    run_id: str
    correlation_id: str
    source: str


@dataclass
class Application:
    name: str
    event_sink: EventSink | None = None
    plugins: list[Plugin] = field(default_factory=list)
    _handler: Handler | None = None

    def use(self, plugin: Plugin) -> None:
        self.plugins.append(plugin)

    def handler(self) -> Callable[[Handler], Handler]:
        def decorator(func: Handler) -> Handler:
            self._handler = func
            return func
        return decorator

    async def emit(self, event_type: str, *, run_id: str | None = None,
                   correlation_id: str | None = None,
                   payload: dict[str, Any] | None = None) -> None:
        event = {
            "type": event_type,
            "timestamp": utcnow(),
            "agent_id": self.name,
            "run_id": run_id,
            "correlation_id": correlation_id,
            "payload": payload or {},
        }
        for plugin in self.plugins:
            await plugin.on_event(event)
        if self.event_sink:
            await self.event_sink(event)

    async def start(self) -> None:
        await self.emit("agent.started")
        for plugin in self.plugins:
            await self.emit("plugin.loaded", payload={"name": plugin.name})
        await self.emit("agent.ready")

    async def stop(self) -> None:
        await self.emit("agent.stopping")
        await self.emit("agent.stopped")

    async def invoke(self, request: Any, *, source: str = "manual",
                     correlation_id: str | None = None) -> Any:
        if self._handler is None:
            raise RuntimeError("handler is not configured")

        run_id = str(uuid.uuid4())
        correlation_id = correlation_id or run_id
        ctx = RunContext(
            agent_id=self.name,
            run_id=run_id,
            correlation_id=correlation_id,
            source=source,
        )
        await self.emit(
            "run.started",
            run_id=run_id,
            correlation_id=correlation_id,
            payload={"source": source},
        )
        try:
            result = self._handler(ctx, request)
            if inspect.isawaitable(result):
                result = await result
            await self.emit(
                "run.completed",
                run_id=run_id,
                correlation_id=correlation_id,
            )
            return result
        except asyncio.CancelledError:
            await self.emit(
                "run.failed",
                run_id=run_id,
                correlation_id=correlation_id,
                payload={"reason": "cancelled"},
            )
            raise
        except Exception as exc:
            await self.emit(
                "run.failed",
                run_id=run_id,
                correlation_id=correlation_id,
                payload={"error": repr(exc)},
            )
            raise
