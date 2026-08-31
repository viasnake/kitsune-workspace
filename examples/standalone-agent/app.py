"""Standalone Kitsune Agent that does not require Workspace."""

from datetime import UTC, datetime

from kitsune import KitsuneApp, RunContext
from pydantic import BaseModel, Field


class EchoRequest(BaseModel):
    """Input accepted by the standalone echo Handler."""

    message: str = Field(min_length=1, max_length=4_096)


class EchoResult(BaseModel):
    """Typed result returned by the standalone echo Handler."""

    message: str
    handled_at: datetime


app = KitsuneApp(agent_id="standalone-agent", version="1.0.0")


@app.handler(
    "echo",
    input_model=EchoRequest,
    output_model=EchoResult,
    description="Return the validated message with its handling time.",
)
async def echo(ctx: RunContext, request: EchoRequest) -> EchoResult:
    """Emit an application event and return the validated message."""

    await ctx.emit("standalone.echo.completed", payload={"message_length": len(request.message)})
    return EchoResult(message=request.message, handled_at=datetime.now(UTC))


if __name__ == "__main__":
    app.run()
