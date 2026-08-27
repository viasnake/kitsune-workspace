"""Ephemeral Agent started for one Workspace Schedule Run."""

import asyncio
from datetime import UTC, datetime

from kitsune import KitsuneApp, RunContext
from kitsune_contracts import UsageRecord
from pydantic import BaseModel, Field


class RefreshRequest(BaseModel):
    """Input supplied by the scheduled Run."""

    collection: str = Field(default="default", min_length=1, max_length=128)
    delay_seconds: float = Field(default=0, ge=0, le=10)
    record_usage: bool = False


class RefreshResult(BaseModel):
    """Result persisted through the terminal Run event."""

    collection: str
    refreshed_at: datetime


app = KitsuneApp(agent_id="scheduled-ephemeral-agent", version="1.0.0")


@app.handler(
    "refresh",
    input_model=RefreshRequest,
    output_model=RefreshResult,
    description="Refresh one collection and exit.",
)
async def refresh(ctx: RunContext, request: RefreshRequest) -> RefreshResult:
    """Emit a domain event and return the refresh timestamp."""

    ctx.raise_if_cancelled()
    if request.delay_seconds:
        await asyncio.sleep(request.delay_seconds)
        ctx.raise_if_cancelled()
    if request.record_usage:
        await ctx.record_usage(
            UsageRecord(
                provider="compose",
                model="deterministic",
                request_count=1,
                input_tokens=2,
                output_tokens=3,
                total_tokens=5,
            )
        )
    await ctx.emit("scheduled.collection.refreshed", payload={"collection": request.collection})
    return RefreshResult(collection=request.collection, refreshed_at=datetime.now(UTC))
