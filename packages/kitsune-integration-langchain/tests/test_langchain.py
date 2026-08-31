"""LangChain Runnable, callback Usage, streaming, trace, and Budget tests."""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from kitsune import EventOutbox, KitsuneApp, KitsuneSettings, RunContext
from kitsune_plugin_budget import BudgetConfiguration, BudgetExceeded, BudgetLimits, BudgetPlugin
from langchain_core.outputs import LLMResult
from langchain_core.runnables import RunnableLambda
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from kitsune_langchain import (
    KitsuneCallbackHandler,
    run_runnable,
    stream_event_payload,
    stream_events,
    usage_to_record,
)


class Request(BaseModel):
    """LangChain test Handler input."""

    value: str


class Result(BaseModel):
    """LangChain test Handler output."""

    value: str


def make_app(tmp_path: Path) -> KitsuneApp:
    """Build an isolated LangChain integration application."""

    return KitsuneApp(
        agent_id="langchain-agent",
        version="1.0.0",
        framework="langchain",
        settings=KitsuneSettings(outbox_path=tmp_path / "events.sqlite3"),
    )


def test_usage_conversion_supports_provider_token_usage() -> None:
    """Provider callback token usage maps to an exact Kitsune Usage Record."""

    result = LLMResult(
        generations=[],
        llm_output={
            "token_usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            }
        },
    )

    record = usage_to_record(result, provider="anthropic", model="chosen-model")

    assert record.provider == "anthropic"
    assert record.input_tokens == 7
    assert record.output_tokens == 3
    assert record.total_tokens == 10
    assert record.estimated_cost is None


def test_stream_events_exposes_only_the_current_langchain_event_api() -> None:
    """The integration fixes LangChain streaming to v2 without a legacy selector."""

    assert "version" not in inspect.signature(stream_events).parameters


def test_stream_event_payload_keeps_identity_without_native_content() -> None:
    """Progress summaries never persist LangChain metadata, input, output, or Tool results."""

    run_id = uuid4()
    parent_id = uuid4()
    payload = stream_event_payload(
        {
            "event": "on_tool_end",
            "name": "lookup",
            "run_id": run_id,
            "parent_ids": [parent_id],
            "tags": ["customer-secret-tag"],
            "metadata": {"tenant": "metadata-secret"},
            "data": {
                "input": {"prompt": "input-secret"},
                "nested": {"output": {"tool_result": "output-secret"}},
            },
        }
    )

    assert payload == {
        "event": "on_tool_end",
        "name": "lookup",
        "run_id": str(run_id),
        "parent_ids": [str(parent_id)],
    }
    serialized = repr(payload)
    assert "metadata-secret" not in serialized
    assert "input-secret" not in serialized
    assert "output-secret" not in serialized
    assert "customer-secret-tag" not in serialized


@pytest.mark.asyncio
async def test_fake_runnable_executes_async_under_kitsune_trace(tmp_path: Path) -> None:
    """A native async Runnable receives correlation metadata under the Run Span."""

    application = make_app(tmp_path)
    observed: dict[str, object] = {}

    async def transform(value: str) -> str:
        observed["trace_valid"] = trace.get_current_span().get_span_context().is_valid
        return value.upper()

    runnable = RunnableLambda(transform)

    @application.handler("transform", input_model=Request, output_model=Result)
    async def transform_handler(ctx: RunContext, request: Request) -> Result:
        value = await run_runnable(runnable, request.value, ctx=ctx)
        return Result(value=value)

    async with application:
        result = await application.execute("transform", {"value": "async"})

    assert result == Result(value="ASYNC")
    assert observed["trace_valid"] is True


@pytest.mark.asyncio
async def test_callback_attaches_usage_and_budget_refuses_model_start(tmp_path: Path) -> None:
    """Callback Usage reaches Run Context and model start uses the Budget guard."""

    application = make_app(tmp_path)
    application.use(BudgetPlugin(BudgetConfiguration(hard=BudgetLimits(model_requests=1))))
    observed_usage: list[int | None] = []

    @application.handler("callback", input_model=Request, output_model=Result)
    async def callback_handler(ctx: RunContext, request: Request) -> Result:
        callback = KitsuneCallbackHandler(ctx)
        first_run_id = uuid4()
        await callback.on_llm_start({}, [request.value], run_id=first_run_id)
        await callback.on_chat_model_start({}, [[]], run_id=first_run_id)
        await callback.on_llm_end(
            LLMResult(
                generations=[],
                llm_output={"token_usage": {"input_tokens": 4, "output_tokens": 2}},
            ),
            run_id=first_run_id,
        )
        observed_usage.append(ctx.usage[-1].total_tokens)
        with pytest.raises(BudgetExceeded):
            await callback.on_llm_start({}, [request.value], run_id=uuid4())
        return Result(value=request.value)

    async with application:
        await application.execute("callback", {"value": "usage"})

    assert observed_usage == [6]


@pytest.mark.asyncio
async def test_failed_callback_releases_usage_capacity_for_next_model(tmp_path: Path) -> None:
    """A failed LangChain model callback does not starve the next tight-cap request."""

    settings = KitsuneSettings(
        outbox_path=tmp_path / "callback-capacity.sqlite3",
        outbox_capacity=4,
    )
    application = KitsuneApp(
        agent_id="langchain-agent",
        version="1.0.0",
        framework="langchain",
        settings=settings,
        outbox=EventOutbox(settings.outbox_path, capacity=4),
    )

    @application.handler("callback", input_model=Request, output_model=Result)
    async def callback_handler(ctx: RunContext, request: Request) -> Result:
        callback = KitsuneCallbackHandler(ctx)
        failed_run_id = uuid4()
        await callback.on_llm_start({}, [request.value], run_id=failed_run_id)
        await callback.on_llm_error(
            RuntimeError("primary unavailable"),
            run_id=failed_run_id,
        )
        fallback_run_id = uuid4()
        await callback.on_llm_start({}, [request.value], run_id=fallback_run_id)
        await callback.on_llm_end(
            LLMResult(
                generations=[],
                llm_output={"token_usage": {"input_tokens": 2, "output_tokens": 1}},
            ),
            run_id=fallback_run_id,
        )
        return Result(value=request.value)

    await application.startup()
    result = await application.execute("callback", {"value": "fallback"})

    assert result == Result(value="fallback")
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


@pytest.mark.asyncio
async def test_native_model_and_tool_callbacks_export_under_kitsune_run(tmp_path: Path) -> None:
    """LangChain Model and Tool callbacks create exported descendants of the Run Span."""

    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    @application.handler("callbacks", input_model=Request, output_model=Result)
    async def callbacks(ctx: RunContext, request: Request) -> Result:
        callback = KitsuneCallbackHandler(ctx)
        model_run_id = uuid4()
        tool_run_id = uuid4()
        await callback.on_llm_start({}, [request.value], run_id=model_run_id)
        await callback.on_llm_end(LLMResult(generations=[]), run_id=model_run_id)
        await callback.on_tool_start(
            {"name": "lookup"},
            "sensitive tool input",
            run_id=tool_run_id,
        )
        await callback.on_tool_end("sensitive tool output", run_id=tool_run_id)
        return Result(value=request.value)

    async with application:
        await application.execute("callbacks", {"value": "sensitive model input"})

    spans = list(exporter.get_finished_spans())
    run_span = next(span for span in spans if span.name == "kitsune.run")
    run_context = run_span.context
    assert run_context is not None
    by_id = {span.context.span_id: span for span in spans if span.context is not None}

    def under_run(span: object) -> bool:
        parent = getattr(span, "parent", None)
        while parent is not None:
            if parent.span_id == run_context.span_id:
                return True
            ancestor = by_id.get(parent.span_id)
            parent = ancestor.parent if ancestor is not None else None
        return False

    model_span = next(span for span in spans if span.name == "kitsune.langchain.model")
    tool_span = next(span for span in spans if span.name == "kitsune.langchain.tool")
    assert under_run(model_span)
    assert under_run(tool_span)
    attributes = repr([dict(model_span.attributes or {}), dict(tool_span.attributes or {})])
    assert "sensitive model input" not in attributes
    assert "sensitive tool input" not in attributes
    assert "sensitive tool output" not in attributes


@pytest.mark.asyncio
async def test_failed_callbacks_and_runnable_export_only_exception_type(tmp_path: Path) -> None:
    """Provider-controlled exception text never enters LangChain spans."""

    secret = "provider-audit-secret"
    application = make_app(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    @application.handler("failures", input_model=Request, output_model=Result)
    async def failures(ctx: RunContext, request: Request) -> Result:
        callback = KitsuneCallbackHandler(ctx)
        model_run_id = uuid4()
        tool_run_id = uuid4()
        await callback.on_llm_start({}, [request.value], run_id=model_run_id)
        await callback.on_llm_error(
            RuntimeError(f"Authorization: Bearer {secret}"),
            run_id=model_run_id,
        )
        await callback.on_tool_start({"name": "lookup"}, request.value, run_id=tool_run_id)
        await callback.on_tool_error(
            ValueError(f"api_key={secret}"),
            run_id=tool_run_id,
        )

        def fail_runnable(_: str) -> str:
            raise LookupError(f"token={secret}")

        with pytest.raises(LookupError, match=secret):
            await run_runnable(RunnableLambda(fail_runnable), request.value, ctx=ctx)
        return Result(value=request.value)

    async with application:
        await application.execute("failures", {"value": "ordinary"})

    spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name
        in {"kitsune.langchain.model", "kitsune.langchain.tool", "kitsune.langchain.run"}
    ]
    exported = [
        {
            "name": span.name,
            "status": span.status.description,
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
        }
        for span in spans
    ]
    serialized = repr(exported)

    assert secret not in serialized
    assert "Authorization" not in serialized
    assert {span["name"] for span in exported} == {
        "kitsune.langchain.model",
        "kitsune.langchain.tool",
        "kitsune.langchain.run",
    }
    assert {
        event["attributes"]["exception.type"] for span in exported for event in span["events"]
    } == {"RuntimeError", "ValueError", "LookupError"}
    assert all(span["status"] is None for span in exported)


@pytest.mark.asyncio
async def test_streaming_events_become_kitsune_progress_events(tmp_path: Path) -> None:
    """Native Runnable stream events are emitted in the Kitsune Run namespace."""

    application = make_app(tmp_path)

    def suffix(value: str) -> str:
        return f"{value}!"

    runnable = RunnableLambda(suffix)
    observed_types: list[str] = []

    @application.handler("stream", input_model=Request, output_model=Result)
    async def stream_handler(ctx: RunContext, request: Request) -> Result:
        async for event in stream_events(runnable, request.value, ctx=ctx):
            observed_types.append(event.type)
        return Result(value=request.value)

    async with application:
        await application.execute("stream", {"value": "progress"})

    assert observed_types
    assert set(observed_types) == {"kitsune.langchain.progress"}
