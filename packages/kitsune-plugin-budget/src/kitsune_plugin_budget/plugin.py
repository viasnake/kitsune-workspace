"""Exact-usage Budget Plugin for SDK-managed model calls and child Runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Self

from kitsune import AppBuilder, KitsuneEvent, PluginMetadata, RunContext, RunningApp, RunOutcome
from kitsune_contracts import UsageRecord
from pydantic import BaseModel, ConfigDict, Field, model_validator

BUDGET_EXTENSION = "kitsune.budget"


class BudgetLimits(BaseModel):
    """Optional independent limits; ``None`` means the dimension is not limited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wall_clock_seconds: float | None = Field(default=None, ge=0)
    model_requests: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: Decimal | None = Field(default=None, ge=0)
    child_runs: int | None = Field(default=None, ge=0)


class BudgetConfiguration(BaseModel):
    """Soft, hard, and finalization-reserve limits for every Run Context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    soft: BudgetLimits = Field(default_factory=BudgetLimits)
    hard: BudgetLimits = Field(default_factory=BudgetLimits)
    finalization_reserve: BudgetLimits = Field(default_factory=BudgetLimits)
    currency: str = "USD"

    @model_validator(mode="after")
    def validate_limit_order(self) -> Self:
        """Require soft limits and reserves not to exceed corresponding hard limits."""

        for dimension in BudgetLimits.model_fields:
            soft = getattr(self.soft, dimension)
            hard = getattr(self.hard, dimension)
            reserve = getattr(self.finalization_reserve, dimension)
            if soft is not None and hard is not None and soft > hard:
                raise ValueError(f"soft {dimension} must not exceed hard {dimension}")
            if reserve is not None:
                if hard is None:
                    raise ValueError(f"finalization reserve for {dimension} requires a hard limit")
                if reserve > hard:
                    raise ValueError(f"finalization reserve for {dimension} exceeds its hard limit")
        if not self.currency.strip():
            raise ValueError("budget currency must not be empty")
        return self


@dataclass(slots=True)
class BudgetState:
    """Exact known consumption and soft-limit state for one Run Context."""

    started_monotonic: float = field(default_factory=monotonic)
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: Decimal = Decimal(0)
    child_runs: int = 0
    soft_dimensions: set[str] = field(default_factory=set[str])
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def wall_clock_seconds(self) -> float:
        """Return measured elapsed monotonic wall-clock time."""

        return monotonic() - self.started_monotonic

    @property
    def soft_limit_reached(self) -> bool:
        """Return whether any soft dimension has been crossed."""

        return bool(self.soft_dimensions)

    def value(self, dimension: str) -> int | float | Decimal:
        """Return current exact consumption for one supported dimension."""

        if dimension == "wall_clock_seconds":
            return self.wall_clock_seconds
        value = getattr(self, dimension)
        if not isinstance(value, int | float | Decimal):
            raise KeyError(dimension)
        return value

    def snapshot(self) -> dict[str, int | float | Decimal | list[str]]:
        """Return a serializable point-in-time budget snapshot."""

        return {
            "wall_clock_seconds": self.wall_clock_seconds,
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "child_runs": self.child_runs,
            "soft_dimensions": sorted(self.soft_dimensions),
        }


class BudgetExceeded(RuntimeError):
    """Typed refusal to start work that would exceed one hard limit."""

    def __init__(
        self,
        *,
        dimension: str,
        used: int | float | Decimal,
        limit: int | float | Decimal,
    ) -> None:
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(f"budget exceeded for {dimension}: {used} >= {limit}")


class BudgetPlugin:
    """Enforce Run budgets through SDK child-Run, model-call, and Usage hooks."""

    def __init__(self, configuration: BudgetConfiguration) -> None:
        self.configuration = configuration
        self.metadata = PluginMetadata(name="kitsune-budget", version="1.0.0", critical=True)
        self._app: RunningApp | None = None
        self._events_observed = 0

    def configure(self, app: AppBuilder) -> None:
        """Install per-Context state and atomic admission/Usage hooks."""

        app.add_context_extension(BUDGET_EXTENSION, lambda _ctx: BudgetState())
        app.add_child_run_guard(self._before_child_run)
        app.add_model_call_guard(self._before_model_call)
        app.add_usage_observer(self._record_usage)

    async def start(self, app: RunningApp) -> None:
        """Bind the running application used for budget lifecycle events."""

        self._app = app
        await app.emit("kitsune.budget.enabled", payload={"currency": self.configuration.currency})

    async def stop(self, app: RunningApp) -> None:
        """Emit an observation summary and release the running application reference."""

        await app.emit("kitsune.budget.stopped", payload={"events_observed": self._events_observed})
        self._app = None

    async def on_run_started(self, ctx: RunContext) -> None:
        """Evaluate time-based soft limits at Run admission."""

        await self._emit_new_soft_limits(ctx, budget_state(ctx))

    async def on_run_finished(self, ctx: RunContext, outcome: RunOutcome) -> None:
        """Emit the Run's final exact budget snapshot."""

        state = budget_state(ctx)
        await self._emit_new_soft_limits(ctx, state)
        await ctx.emit(
            "kitsune.budget.final",
            payload={
                "status": outcome.status.value,
                "usage": _json_snapshot(state.snapshot()),
            },
        )

    async def on_event(self, event: KitsuneEvent) -> None:
        """Count observed events without reading or mutating SDK internals."""

        self._events_observed += 1

    async def _before_child_run(self, ctx: RunContext) -> None:
        state = budget_state(ctx)
        async with state._lock:  # pyright: ignore[reportPrivateUsage]
            self._check_hard_limits(state, operation="child", finalization=False)
            projected = state.child_runs + 1
            child_limit = self.configuration.hard.child_runs
            if child_limit is not None and projected > child_limit:
                raise BudgetExceeded(dimension="child_runs", used=projected, limit=child_limit)
            state.child_runs = projected
            await self._emit_new_soft_limits(ctx, state)

    async def _before_model_call(self, ctx: RunContext, finalization: bool) -> None:
        state = budget_state(ctx)
        async with state._lock:  # pyright: ignore[reportPrivateUsage]
            self._check_hard_limits(state, operation="model", finalization=finalization)
            hard_limit = self._available_limit("model_requests", finalization)
            projected = state.model_requests + 1
            if hard_limit is not None and projected > hard_limit:
                raise BudgetExceeded(dimension="model_requests", used=projected, limit=hard_limit)
            state.model_requests = projected
            await self._emit_new_soft_limits(ctx, state)

    async def _record_usage(self, ctx: RunContext, usage: UsageRecord) -> None:
        state = budget_state(ctx)
        async with state._lock:  # pyright: ignore[reportPrivateUsage]
            if usage.request_count is not None:
                state.model_requests = max(state.model_requests, usage.request_count)
            if usage.input_tokens is not None:
                state.input_tokens += usage.input_tokens
            if usage.output_tokens is not None:
                state.output_tokens += usage.output_tokens
            if usage.total_tokens is not None:
                state.total_tokens += usage.total_tokens
            if (
                usage.estimated_cost is not None
                and usage.currency is not None
                and usage.currency.casefold() == self.configuration.currency.casefold()
            ):
                state.estimated_cost += usage.estimated_cost
            await self._emit_new_soft_limits(ctx, state)

    def _check_hard_limits(self, state: BudgetState, *, operation: str, finalization: bool) -> None:
        dimensions: tuple[str, ...] = (
            "wall_clock_seconds",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "estimated_cost",
        )
        if operation == "model":
            dimensions = (*dimensions, "model_requests")
        else:
            dimensions = (*dimensions, "child_runs")
        for dimension in dimensions:
            limit = self._available_limit(dimension, finalization)
            if limit is None:
                continue
            used = state.value(dimension)
            if used >= limit:
                raise BudgetExceeded(dimension=dimension, used=used, limit=limit)

    def _available_limit(self, dimension: str, finalization: bool) -> int | float | Decimal | None:
        hard = getattr(self.configuration.hard, dimension)
        if hard is None or finalization:
            return hard
        reserve = getattr(self.configuration.finalization_reserve, dimension)
        if reserve is None:
            return hard
        return hard - reserve

    async def _emit_new_soft_limits(self, ctx: RunContext, state: BudgetState) -> None:
        for dimension in BudgetLimits.model_fields:
            limit = getattr(self.configuration.soft, dimension)
            if limit is None or dimension in state.soft_dimensions:
                continue
            used = state.value(dimension)
            if used >= limit:
                state.soft_dimensions.add(dimension)
                await ctx.emit(
                    "kitsune.budget.soft_limit",
                    payload={
                        "dimension": dimension,
                        "used": _json_number(used),
                        "limit": _json_number(limit),
                    },
                )


def budget_state(ctx: RunContext) -> BudgetState:
    """Return the typed Budget extension installed on ``ctx``."""

    state = ctx.extension(BUDGET_EXTENSION)
    if not isinstance(state, BudgetState):
        raise TypeError("Run Context does not contain a valid Kitsune Budget state")
    return state


def _json_number(value: int | float | Decimal) -> int | float | str:
    return str(value) if isinstance(value, Decimal) else value


def _json_snapshot(
    snapshot: dict[str, int | float | Decimal | list[str]],
) -> dict[str, int | float | str | list[str]]:
    return {
        key: _json_number(value) if isinstance(value, int | float | Decimal) else value
        for key, value in snapshot.items()
    }
