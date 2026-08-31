"""Pydantic AI Model, TestModel, Usage, trace, and Budget integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kitsune import EventOutbox, KitsuneApp, KitsuneSettings, RunContext
from kitsune_plugin_budget import BudgetConfiguration, BudgetExceeded, BudgetLimits, BudgetPlugin
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings, InstrumentedModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RunUsage

from kitsune_pydantic_ai import (
    MCPServerConfiguration,
    ProviderModelConfiguration,
    create_test_model,
    resolve_model,
    run_agent,
    usage_to_record,
)


class Request(BaseModel):
    """Pydantic AI test Handler input."""

    prompt: str


class Result(BaseModel):
    """Pydantic AI test Handler output."""

    text: str


def make_app(tmp_path: Path) -> KitsuneApp:
    """Build an isolated Pydantic AI integration application."""

    return KitsuneApp(
        agent_id="pydantic-agent",
        version="1.0.0",
        framework="pydantic-ai",
        settings=KitsuneSettings(outbox_path=tmp_path / "events.sqlite3"),
    )


def test_provider_and_fallback_model_resolution() -> None:
    """Provider identifiers remain caller-selected and fallbacks use Pydantic AI natively."""

    primary = TestModel(model_name="primary")
    fallback = TestModel(model_name="fallback")

    assert ProviderModelConfiguration(provider="openai", model="custom-model").identifier == (
        "openai:custom-model"
    )
    single = resolve_model(primary)
    assert getattr(single, "wrapped", None) is primary
    resolved = resolve_model(primary, [fallback])
    assert isinstance(resolved, FallbackModel)
    assert [getattr(model, "wrapped", None) for model in resolved.models] == [
        primary,
        fallback,
    ]


def test_mcp_configuration_builds_native_server() -> None:
    """MCP settings are translated to Pydantic AI's implementation, not reimplemented."""

    toolset = MCPServerConfiguration(
        transport="stdio", command="python", args=("-m", "example_mcp")
    ).build()
    transport = toolset.client.transport

    assert transport.command == "python"
    assert list(transport.args) == ["-m", "example_mcp"]


def test_usage_conversion_preserves_only_known_values() -> None:
    """Usage conversion carries exact token/cache counts and leaves cost unavailable."""

    usage = RunUsage(
        requests=2,
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=3,
        cache_write_tokens=1,
    )

    record = usage_to_record(usage, provider="openai", model="caller-model")

    assert record.request_count == 2
    assert record.total_tokens == 14
    assert record.cache_read_tokens == 3
    assert record.cache_write_tokens == 1
    assert record.estimated_cost is None


@pytest.mark.asyncio
async def test_testmodel_runs_offline_and_records_usage_under_run_trace(tmp_path: Path) -> None:
    """Native TestModel execution remains under the current Kitsune Run Span."""

    application = make_app(tmp_path)
    agent = Agent(create_test_model(custom_output_text="offline"), output_type=str)
    trace_was_valid = False

    @application.handler("ask", input_model=Request, output_model=Result)
    async def ask(ctx: RunContext, request: Request) -> Result:
        nonlocal trace_was_valid
        trace_was_valid = trace.get_current_span().get_span_context().is_valid
        result = await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text=result.output)

    async with application:
        result = await application.execute("ask", {"prompt": "hello"})

    assert result == Result(text="offline")
    assert trace_was_valid


@pytest.mark.asyncio
async def test_native_model_and_tool_spans_export_under_kitsune_run(tmp_path: Path) -> None:
    """Pydantic AI native Model and Tool spans share the exported Kitsune Run trace."""

    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = Agent(TestModel(), output_type=str)

    @agent.tool_plain
    def lookup() -> str:
        return "sensitive tool result"

    @application.handler("instrumented", input_model=Request, output_model=Result)
    async def instrumented(ctx: RunContext, request: Request) -> Result:
        result = await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text=result.output)

    async with application:
        await application.execute("instrumented", {"prompt": "sensitive prompt"})

    spans = list(exporter.get_finished_spans())
    run_span = next(span for span in spans if span.name == "kitsune.run")
    run_context = run_span.context
    assert run_context is not None
    by_id = {span.context.span_id: span for span in spans if span.context is not None}

    def under_run(span: Any) -> bool:
        parent = span.parent
        while parent is not None:
            if parent.span_id == run_context.span_id:
                return True
            ancestor = by_id.get(parent.span_id)
            parent = ancestor.parent if ancestor is not None else None
        return False

    model_spans = [
        span for span in spans if (span.attributes or {}).get("gen_ai.operation.name") == "chat"
    ]
    tool_spans = [
        span
        for span in spans
        if (span.attributes or {}).get("gen_ai.operation.name") == "execute_tool"
    ]
    assert model_spans and tool_spans
    assert all(under_run(span) for span in [*model_spans, *tool_spans])
    attributes = repr([dict(span.attributes or {}) for span in [*model_spans, *tool_spans]])
    assert "sensitive prompt" not in attributes
    assert "sensitive tool result" not in attributes


@pytest.mark.asyncio
async def test_failed_pydantic_run_exports_only_exception_type(tmp_path: Path) -> None:
    """Provider exception text never enters Kitsune's Pydantic AI execution span."""

    secret = "pydantic-span-sentinel"

    async def unavailable(messages: Any, agent_info: Any) -> Any:
        del messages, agent_info
        raise ModelAPIError("private-provider", f"Authorization: Bearer {secret}")

    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with pytest.warns(UserWarning, match="instrument"):
        agent = Agent(  # pyright: ignore[reportCallIssue] - regression for deprecated override
            FunctionModel(unavailable, model_name="private-provider"),
            output_type=str,
            instrument=True,
        )

    @application.handler("fail", input_model=Request, output_model=Result)
    async def fail(ctx: RunContext, request: Request) -> Result:
        await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text="unreachable")

    async with application:
        with pytest.raises(ModelAPIError, match=secret):
            await application.execute("fail", {"prompt": "ordinary"})

    spans = list(exporter.get_finished_spans())
    span = next(item for item in spans if item.name == "kitsune.pydantic_ai.run")
    serialized = repr(
        [
            {
                "name": item.name,
                "status": item.status.description,
                "attributes": dict(item.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in item.events
                ],
            }
            for item in spans
        ]
    )
    assert secret not in serialized
    assert any((item.attributes or {}).get("gen_ai.operation.name") == "chat" for item in spans)
    assert span.events[0].attributes == {"exception.type": "ModelAPIError"}


@pytest.mark.asyncio
async def test_explicit_instrumentation_capability_is_rejected(tmp_path: Path) -> None:
    """Callers cannot replace Kitsune's exception-safe native instrumentation capability."""

    agent = Agent(
        TestModel(custom_output_text="never called"),
        output_type=str,
        capabilities=[Instrumentation()],
    )
    application = make_app(tmp_path)

    @application.handler("reject", input_model=Request, output_model=Result)
    async def reject(ctx: RunContext, request: Request) -> Result:
        await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text="unreachable")

    async with application:
        with pytest.raises(ValueError, match="exception-safe native instrumentation"):
            await application.execute("reject", {"prompt": "ordinary"})


@pytest.mark.asyncio
async def test_nested_unsafe_instrumented_model_is_rewritten_before_execution(
    tmp_path: Path,
) -> None:
    """Generic native wrappers cannot hide content-enabled instrumentation from Kitsune."""

    prompt_secret = "nested-pydantic-prompt-sentinel"
    exception_secret = "nested-pydantic-exception-sentinel"

    async def unavailable(messages: Any, agent_info: Any) -> Any:
        del messages, agent_info
        raise ModelAPIError("private-provider", exception_secret)

    unsafe = InstrumentedModel(
        FunctionModel(unavailable, model_name="private-provider"),
        InstrumentationSettings(include_content=True, include_binary_content=True),
    )
    agent = Agent(WrapperModel(unsafe), output_type=str)
    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    @application.handler("nested", input_model=Request, output_model=Result)
    async def nested(ctx: RunContext, request: Request) -> Result:
        await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text="unreachable")

    async with application:
        with pytest.raises(ModelAPIError, match=exception_secret):
            await application.execute("nested", {"prompt": prompt_secret})

    spans = list(exporter.get_finished_spans())
    serialized = repr(
        [
            {
                "name": item.name,
                "status": item.status.description,
                "attributes": dict(item.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in item.events
                ],
            }
            for item in spans
        ]
    )
    assert prompt_secret not in serialized
    assert exception_secret not in serialized
    assert any((item.attributes or {}).get("gen_ai.operation.name") == "chat" for item in spans)


@pytest.mark.asyncio
async def test_unknown_wrapper_with_unsafe_alias_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    """Unknown wrappers cannot retain an alternate reference to unsafe instrumentation."""

    prompt_secret = "alias-wrapper-prompt-sentinel"
    exception_secret = "alias-wrapper-exception-sentinel"

    async def unavailable(messages: Any, agent_info: Any) -> Any:
        del messages, agent_info
        raise ModelAPIError("private-provider", exception_secret)

    class AliasWrapper(WrapperModel):
        def __init__(self, wrapped: Model) -> None:
            super().__init__(wrapped)
            self.delegate = wrapped

        async def request(
            self,
            messages: list[ModelMessage],
            model_settings: Any,
            model_request_parameters: ModelRequestParameters,
        ) -> ModelResponse:
            return await self.delegate.request(
                messages,
                model_settings,
                model_request_parameters,
            )

    unsafe = InstrumentedModel(
        FunctionModel(unavailable, model_name="private-provider"),
        InstrumentationSettings(include_content=True, include_binary_content=True),
    )
    agent = Agent(AliasWrapper(unsafe), output_type=str)
    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    @application.handler("alias", input_model=Request, output_model=Result)
    async def alias(ctx: RunContext, request: Request) -> Result:
        await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text="unreachable")

    async with application:
        with pytest.raises(ValueError, match=r"unsupported.*AliasWrapper"):
            await application.execute("alias", {"prompt": prompt_secret})

    spans = list(exporter.get_finished_spans())
    serialized = repr(
        [
            {
                "name": item.name,
                "status": item.status.description,
                "attributes": dict(item.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in item.events
                ],
            }
            for item in spans
        ]
    )
    assert prompt_secret not in serialized
    assert exception_secret not in serialized
    assert not any((item.attributes or {}).get("gen_ai.operation.name") for item in spans)


@pytest.mark.asyncio
async def test_budget_guard_refuses_pydantic_ai_model_call(tmp_path: Path) -> None:
    """Pydantic AI execution checks the typed Budget guard before calling the Agent."""

    application = make_app(tmp_path)
    application.use(BudgetPlugin(BudgetConfiguration(hard=BudgetLimits(model_requests=0))))

    class NeverCalledAgent:
        model = TestModel()

        async def run(self, prompt: Any, **kwargs: Any) -> Any:
            raise AssertionError("hard budget must refuse before Agent.run")

    @application.handler("ask", input_model=Request, output_model=Result)
    async def ask(ctx: RunContext, request: Request) -> Result:
        await run_agent(NeverCalledAgent(), request.prompt, ctx=ctx)
        return Result(text="unreachable")

    async with application:
        with pytest.raises(BudgetExceeded) as raised:
            await application.execute("ask", {"prompt": "blocked"})

    assert raised.value.dimension == "model_requests"


@pytest.mark.asyncio
async def test_budget_guards_each_fallback_provider_attempt(tmp_path: Path) -> None:
    """A failed primary attempt consumes admission before native fallback selection."""

    async def unavailable(messages: Any, agent_info: Any) -> Any:
        raise ModelAPIError("primary", "unavailable")

    fallback = FallbackModel(
        FunctionModel(unavailable, model_name="primary"),
        TestModel(custom_output_text="fallback", model_name="fallback"),
    )
    original_models = tuple(fallback.models)
    agent = Agent(fallback, output_type=str)
    application = make_app(tmp_path)
    application.use(BudgetPlugin(BudgetConfiguration(hard=BudgetLimits(model_requests=1))))

    @application.handler("fallback", input_model=Request, output_model=Result)
    async def fallback_handler(ctx: RunContext, request: Request) -> Result:
        result = await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text=result.output)

    async with application:
        with pytest.raises(BudgetExceeded) as raised:
            await application.execute("fallback", {"prompt": "try both"})

    assert raised.value.dimension == "model_requests"
    assert len(fallback.models) == len(original_models)
    assert all(
        current is original
        for current, original in zip(fallback.models, original_models, strict=True)
    )


@pytest.mark.asyncio
async def test_failed_primary_releases_usage_capacity_for_native_fallback(
    tmp_path: Path,
) -> None:
    """A native fallback can reserve Usage durability after its primary provider fails."""

    async def unavailable(messages: Any, agent_info: Any) -> Any:
        raise ModelAPIError("primary", "unavailable")

    fallback = FallbackModel(
        FunctionModel(unavailable, model_name="primary"),
        TestModel(custom_output_text="fallback", model_name="fallback"),
    )
    agent = Agent(fallback, output_type=str)
    settings = KitsuneSettings(
        outbox_path=tmp_path / "fallback-capacity.sqlite3",
        outbox_capacity=4,
    )
    application = KitsuneApp(
        agent_id="pydantic-agent",
        version="1.0.0",
        framework="pydantic-ai",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )

    @application.handler("fallback", input_model=Request, output_model=Result)
    async def fallback_handler(ctx: RunContext, request: Request) -> Result:
        result = await run_agent(agent, request.prompt, ctx=ctx)
        return Result(text=result.output)

    await application.startup()
    result = await application.execute("fallback", {"prompt": "try both"})

    assert result == Result(text="fallback")
    assert application.outbox is not None
    pending = await application.outbox.pending(limit=10)
    assert [item.event.type for item in pending] == [
        "kitsune.agent.started",
        "kitsune.run.started",
        "kitsune.model.usage",
        "kitsune.run.succeeded",
    ]
    await application.outbox.mark_delivered([item.event.event_id for item in pending])
    await application.shutdown()
