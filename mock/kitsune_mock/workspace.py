from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sdk import Application


@dataclass
class MockWorkspace:
    agents: dict[str, Application] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    async def ingest(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def register(self, app: Application) -> None:
        app.event_sink = self.ingest
        self.agents[app.name] = app

    async def start_all(self) -> None:
        for app in self.agents.values():
            await app.start()

    async def stop_all(self) -> None:
        for app in reversed(list(self.agents.values())):
            await app.stop()

    async def invoke(self, agent_id: str, request: Any) -> Any:
        return await self.agents[agent_id].invoke(request, source="workspace.manual")

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for agent_id in self.agents:
            relevant = [e for e in self.events if e["agent_id"] == agent_id]
            result[agent_id] = {
                "event_count": len(relevant),
                "last_event": relevant[-1]["type"] if relevant else None,
                "runs": len([e for e in relevant if e["type"] == "run.started"]),
            }
        return result
