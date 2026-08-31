"""Resident Agent managed through the Kitsune Control API."""

import asyncio
from datetime import UTC, datetime

from kitsune import KitsuneApp, RunContext
from pydantic import BaseModel, Field


class InvestigationRequest(BaseModel):
    """A bounded request for the managed investigation example."""

    message: str = Field(min_length=1, max_length=16_384)
    depth: int = Field(default=1, ge=1, le=5)


class InvestigationResult(BaseModel):
    """Summary returned by the investigation example."""

    summary: str
    completed_at: datetime


class RefreshRequest(BaseModel):
    """Input for refreshing resident Agent state."""

    scope: str = Field(default="all", min_length=1, max_length=128)


class RefreshResult(BaseModel):
    """Result of one resident refresh."""

    refreshed_scope: str
    completed_at: datetime


app = KitsuneApp(agent_id="managed-resident-agent", version="1.0.0")


@app.handler(
    "investigate",
    input_model=InvestigationRequest,
    output_model=InvestigationResult,
    description="Perform a deterministic managed investigation.",
)
async def investigate(ctx: RunContext, request: InvestigationRequest) -> InvestigationResult:
    """Demonstrate progress events and cooperative cancellation."""

    for step in range(request.depth):
        ctx.raise_if_cancelled()
        await asyncio.sleep(0.05)
        await ctx.emit(
            "managed.investigation.progress",
            payload={"completed_steps": step + 1, "total_steps": request.depth},
        )
    return InvestigationResult(
        summary=f"Processed: {request.message}",
        completed_at=datetime.now(UTC),
    )


@app.handler(
    "refresh",
    input_model=RefreshRequest,
    output_model=RefreshResult,
    description="Refresh one declared state scope.",
)
async def refresh(ctx: RunContext, request: RefreshRequest) -> RefreshResult:
    """Return a typed refresh result without external dependencies."""

    ctx.raise_if_cancelled()
    await ctx.emit("managed.refresh.completed", payload={"scope": request.scope})
    return RefreshResult(refreshed_scope=request.scope, completed_at=datetime.now(UTC))


if __name__ == "__main__":
    app.run()
