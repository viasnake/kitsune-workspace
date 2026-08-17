from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class Plugin(Protocol):
    name: str

    async def on_event(self, event: dict[str, Any]) -> None: ...


@dataclass
class UsagePlugin:
    name: str = "usage"
    total_units: int = 0

    async def on_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "model.usage":
            self.total_units += int(event["payload"].get("units", 0))
