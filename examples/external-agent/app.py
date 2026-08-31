"""External Agent whose process lifecycle is not controlled by Workspace."""

from datetime import UTC, datetime

from kitsune import KitsuneApp, RunContext
from pydantic import BaseModel, Field


class ExternalRequest(BaseModel):
    """Input accepted by the externally hosted Agent."""

    payload: dict[str, object] = Field(default_factory=dict)


class ExternalResult(BaseModel):
    """Acknowledgement returned by the external Agent."""

    accepted_keys: list[str]
    completed_at: datetime


app = KitsuneApp(agent_id="external-agent", version="1.0.0")


@app.handler(
    "receive",
    input_model=ExternalRequest,
    output_model=ExternalResult,
    description="Accept an external payload without changing its domain shape.",
)
async def receive(ctx: RunContext, request: ExternalRequest) -> ExternalResult:
    """Preserve the payload boundary and report only operational metadata."""

    keys = sorted(request.payload)
    await ctx.emit("external.payload.received", payload={"keys": keys})
    return ExternalResult(accepted_keys=keys, completed_at=datetime.now(UTC))


if __name__ == "__main__":
    app.run()
