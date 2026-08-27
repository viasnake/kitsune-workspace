"""Budget soft/hard, reserve, child Run, exact Usage, and unknown-value tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from kitsune import (
    AppBuilder,
    EventOutbox,
    KitsuneApp,
    KitsuneEvent,
    KitsuneSettings,
    OutboxFullError,
    PluginMetadata,
    RunContext,
    RunningApp,
    RunOutcome,
)
from kitsune_contracts import UsageRecord
from pydantic import BaseModel

from kitsune_plugin_budget import (
    BudgetConfiguration,
    BudgetExceeded,
    BudgetLimits,
    BudgetPlugin,
    budget_state,
)


class Request(BaseModel):
    """Budget test input."""

    value: str = "run"


class Result(BaseModel):
    """Budget test output."""

    soft: bool


class EventCapture:
    """Capture emitted Budget Events using only stable Plugin hooks."""

    metadata = PluginMetadata(name="event-capture", version="1.0.0")

    def __init__(self) -> None:
        self.events: list[KitsuneEvent] = []

    def configure(self, app: AppBuilder) -> None:
        self.agent_id = app.agent_id

    async def start(self, app: RunningApp) -> None:
        self.started_agent = app.agent_id

    async def stop(self, app: RunningApp) -> None:
        self.stopped_agent = app.agent_id

    async def on_run_started(self, ctx: RunContext) -> None:
        self.last_run = ctx.run_id

    async def on_run_finished(self, ctx: RunContext, outcome: RunOutcome) -> None:
        self.last_outcome = outcome.status

    async def on_event(self, event: KitsuneEvent) -> None:
        self.events.append(event)


def make_app(tmp_path: Path, configuration: BudgetConfiguration) -> tuple[KitsuneApp, EventCapture]:
    """Build an application with Budget and Event-capture Plugins."""

    app = KitsuneApp(
        agent_id="budget-agent",
        version="1.0.0",
        settings=KitsuneSettings(outbox_path=tmp_path / "events.sqlite3"),
    )
    capture = EventCapture()
    app.use(BudgetPlugin(configuration)).use(capture)
    return app, capture


@pytest.mark.asyncio
async def test_soft_limit_emits_once_and_is_visible_on_context(tmp_path: Path) -> None:
    """Crossing a soft Token limit emits once and never interrupts the Handler."""

    app, capture = make_app(
        tmp_path,
        BudgetConfiguration(soft=BudgetLimits(total_tokens=5)),
    )

    @app.handler("usage", input_model=Request, output_model=Result)
    async def usage(ctx: RunContext, request: Request) -> Result:
        await ctx.record_usage(UsageRecord(total_tokens=5))
        await ctx.record_usage(UsageRecord(total_tokens=1))
        return Result(soft=ctx.soft_limit_reached)

    async with app:
        result = Result.model_validate(await app.execute("usage", {}))

    assert result.soft
    soft_events = [event for event in capture.events if event.type == "kitsune.budget.soft_limit"]
    assert len(soft_events) == 1
    assert soft_events[0].payload["dimension"] == "total_tokens"


@pytest.mark.asyncio
async def test_hard_limits_refuse_new_model_and_child_work(tmp_path: Path) -> None:
    """Hard limits return typed failures before new SDK-managed work starts."""

    app, _ = make_app(
        tmp_path,
        BudgetConfiguration(hard=BudgetLimits(model_requests=0, child_runs=0)),
    )

    @app.handler("hard", input_model=Request, output_model=Result)
    async def hard(ctx: RunContext, request: Request) -> Result:
        with pytest.raises(BudgetExceeded) as model_error:
            await ctx.check_model_call()
        assert model_error.value.dimension == "model_requests"
        with pytest.raises(BudgetExceeded) as child_error:
            async with ctx.child_run(name="blocked"):
                raise AssertionError("child body must not start")
        assert child_error.value.dimension == "child_runs"
        return Result(soft=False)

    async with app:
        await app.execute("hard", {})


@pytest.mark.asyncio
async def test_outbox_admission_failure_does_not_consume_model_or_child_budget(
    tmp_path: Path,
) -> None:
    """Durability refusal happens before Budget mutates either admission counter."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "bounded.sqlite3",
        outbox_capacity=4,
    )
    app = KitsuneApp(
        agent_id="budget-agent",
        version="1.0.0",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )
    app.use(BudgetPlugin(BudgetConfiguration()))

    @app.handler("bounded", input_model=Request, output_model=Result)
    async def bounded(ctx: RunContext, request: Request) -> Result:
        state = budget_state(ctx)
        with pytest.raises(OutboxFullError):
            await ctx.check_model_call()
        assert state.model_requests == 0
        with pytest.raises(OutboxFullError):
            async with ctx.child_run(name="not-admitted"):
                raise AssertionError("child body must not run")
        assert state.child_runs == 0
        return Result(soft=False)

    await app.startup()
    await app.execute("bounded", {})

    assert app.outbox is not None
    pending = await app.outbox.pending(limit=10)
    assert {item.event.type for item in pending} >= {
        "kitsune.run.started",
        "kitsune.run.succeeded",
    }
    await app.outbox.mark_delivered([item.event.event_id for item in pending])
    await app.shutdown()


@pytest.mark.asyncio
async def test_finalization_reserve_is_available_only_when_requested(tmp_path: Path) -> None:
    """Normal calls cannot consume the Model request reserved for finalization."""

    app, _ = make_app(
        tmp_path,
        BudgetConfiguration(
            hard=BudgetLimits(model_requests=2),
            finalization_reserve=BudgetLimits(model_requests=1),
        ),
    )

    @app.handler("reserve", input_model=Request, output_model=Result)
    async def reserve(ctx: RunContext, request: Request) -> Result:
        await ctx.check_model_call()
        with pytest.raises(BudgetExceeded):
            await ctx.check_model_call()
        await ctx.check_model_call(finalization=True)
        return Result(soft=False)

    async with app:
        await app.execute("reserve", {})


@pytest.mark.asyncio
async def test_unknown_usage_and_other_currency_are_not_guessed(tmp_path: Path) -> None:
    """Unavailable Tokens and incomparable estimated cost do not trigger hard refusal."""

    app, _ = make_app(
        tmp_path,
        BudgetConfiguration(
            hard=BudgetLimits(total_tokens=1, estimated_cost=Decimal("0.01")),
            currency="USD",
        ),
    )

    @app.handler("unknown", input_model=Request, output_model=Result)
    async def unknown(ctx: RunContext, request: Request) -> Result:
        await ctx.record_usage(UsageRecord(provider="offline"))
        await ctx.record_usage(UsageRecord(estimated_cost=Decimal("100"), currency="JPY"))
        await ctx.check_model_call()
        state = budget_state(ctx)
        assert state.total_tokens == 0
        assert state.estimated_cost == 0
        return Result(soft=False)

    async with app:
        await app.execute("unknown", {})
