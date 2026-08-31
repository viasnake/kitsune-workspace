"""LangChain integration built on native Runnable, callback, and event APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal, cast
from uuid import UUID

from kitsune import ModelCallAdmission, RunContext
from kitsune_contracts import KitsuneEvent, UsageRecord
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.runnables.config import merge_configs
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, ConfigDict, Field


def _record_safe_exception(span: Span, error: BaseException) -> None:
    """Record failure classification without exporting provider-controlled text."""

    span.add_event("exception", {"exception.type": type(error).__name__})
    span.set_status(Status(StatusCode.ERROR))


class LangChainModelConfiguration(BaseModel):
    """Provider-neutral input to LangChain's native ``init_chat_model`` helper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    provider: Literal["openai", "anthropic"] | None = None
    configurable_fields: tuple[str, ...] = ()

    def resolve(self, **kwargs: Any) -> Any:
        """Resolve a native LangChain chat model without wrapping its provider package."""

        return resolve_chat_model(
            self.model,
            provider=self.provider,
            configurable_fields=self.configurable_fields,
            **kwargs,
        )


class KitsuneCallbackHandler(AsyncCallbackHandler):
    """Capture native LangChain LLM usage and enforce Kitsune model-call budgets."""

    raise_error = True

    def __init__(self, ctx: RunContext, *, finalization: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self.finalization = finalization
        self._started_model_runs: set[UUID] = set()
        self._model_admissions: dict[UUID, ModelCallAdmission] = {}
        self._model_spans: dict[UUID, Span] = {}
        self._tool_spans: dict[UUID, Span] = {}
        self._start_lock = asyncio.Lock()

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Check the hard budget immediately before each native LLM request."""

        del serialized, prompts, parent_run_id, tags, metadata, kwargs
        await self._guard_once(run_id)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Apply the same budget guard to native chat-model requests."""

        del serialized, messages, parent_run_id, tags, metadata, kwargs
        await self._guard_once(run_id)

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Convert and attach exact usage exposed by a LangChain LLM result."""

        del parent_run_id, kwargs
        record = usage_to_record(response)
        admission = self._model_admissions.pop(run_id, None)
        try:
            if _has_known_usage(record):
                await self.ctx.record_usage(record)
            elif admission is not None:
                admission.release()
        except BaseException:
            if admission is not None:
                admission.release()
            raise
        finally:
            self._started_model_runs.discard(run_id)
            span = self._model_spans.pop(run_id, None)
            if span is not None:
                span.end()

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close a native model callback Span with its failure status."""

        del parent_run_id, kwargs
        admission = self._model_admissions.pop(run_id, None)
        if admission is not None:
            admission.release()
        self._started_model_runs.discard(run_id)
        span = self._model_spans.pop(run_id, None)
        if span is not None:
            _record_safe_exception(span, error)
            span.end()

    async def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a Tool Span from LangChain's native callback lifecycle."""

        del input_str, parent_run_id, tags, metadata, inputs, kwargs
        span = self.ctx.tracer.start_span("kitsune.langchain.tool")
        name = (serialized or {}).get("name")
        if isinstance(name, str):
            span.set_attribute("gen_ai.tool.name", name)
        self._tool_spans[run_id] = span

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close a successful native Tool callback Span."""

        del output, parent_run_id, kwargs
        span = self._tool_spans.pop(run_id, None)
        if span is not None:
            span.end()

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close a failed native Tool callback Span."""

        del parent_run_id, kwargs
        span = self._tool_spans.pop(run_id, None)
        if span is not None:
            _record_safe_exception(span, error)
            span.end()

    async def _guard_once(self, run_id: UUID) -> None:
        async with self._start_lock:
            if run_id in self._started_model_runs:
                return
            admission = await self.ctx.check_model_call(finalization=self.finalization)
            try:
                span = self.ctx.tracer.start_span("kitsune.langchain.model")
            except BaseException:
                admission.release()
                raise
            self._started_model_runs.add(run_id)
            self._model_admissions[run_id] = admission
            self._model_spans[run_id] = span


async def run_runnable[InputT, OutputT](
    runnable: Runnable[InputT, OutputT],
    input_data: InputT,
    *,
    ctx: RunContext,
    config: RunnableConfig | None = None,
    finalization: bool = False,
) -> OutputT:
    """Invoke a native Runnable asynchronously under Kitsune trace and callbacks."""

    callback = KitsuneCallbackHandler(ctx, finalization=finalization)
    effective = _merge_config(config, callback, ctx)
    with ctx.tracer.start_as_current_span(
        "kitsune.langchain.run", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("kitsune.agent.id", ctx.agent_id)
        span.set_attribute("kitsune.run.id", str(ctx.run_id))
        try:
            return await runnable.ainvoke(input_data, config=effective)
        except Exception as error:
            _record_safe_exception(span, error)
            raise


async def stream_events[InputT, OutputT](
    runnable: Runnable[InputT, OutputT],
    input_data: InputT,
    *,
    ctx: RunContext,
    config: RunnableConfig | None = None,
    finalization: bool = False,
) -> AsyncIterator[KitsuneEvent]:
    """Convert native LangChain v2 stream events to Kitsune progress events."""

    callback = KitsuneCallbackHandler(ctx, finalization=finalization)
    effective = _merge_config(config, callback, ctx)
    with ctx.tracer.start_as_current_span(
        "kitsune.langchain.stream", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("kitsune.agent.id", ctx.agent_id)
        span.set_attribute("kitsune.run.id", str(ctx.run_id))
        try:
            async for event in runnable.astream_events(input_data, config=effective, version="v2"):
                payload = stream_event_payload(event)
                yield await ctx.emit("kitsune.langchain.progress", payload=payload)
        except Exception as error:
            _record_safe_exception(span, error)
            raise


def stream_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return structural progress identity without native prompt or result content."""

    return {
        "event": str(event.get("event", "unknown")),
        "name": str(event.get("name", "")),
        "run_id": str(event.get("run_id", "")),
        "parent_ids": [str(item) for item in event.get("parent_ids", [])],
    }


def usage_to_record(
    response: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> UsageRecord:
    """Convert known LangChain usage metadata without estimating absent fields."""

    usage = _find_usage(response)
    provider = provider or _nested_string(usage, "provider")
    model = model or _nested_string(usage, "model_name", "model")
    input_tokens = _mapping_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = _mapping_int(usage, "output_tokens", "completion_tokens")
    total_tokens = _mapping_int(usage, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    details = usage.get("input_token_details")
    return UsageRecord(
        provider=provider,
        model=model,
        request_count=1 if usage else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=_mapping_int(details, "cache_read", "cache_read_tokens"),
        cache_write_tokens=_mapping_int(details, "cache_creation", "cache_write_tokens"),
    )


def resolve_chat_model(
    model: str,
    *,
    provider: str | None = None,
    configurable_fields: tuple[str, ...] = (),
    **kwargs: Any,
) -> Any:
    """Delegate Model creation to LangChain's native configurable Model resolver."""

    from langchain.chat_models import init_chat_model

    return init_chat_model(
        model,
        model_provider=provider,
        configurable_fields=configurable_fields or None,
        **kwargs,
    )


def _merge_config(
    config: RunnableConfig | None,
    callback: KitsuneCallbackHandler,
    ctx: RunContext,
) -> RunnableConfig:
    instrumentation: RunnableConfig = {
        "callbacks": [callback],
        "metadata": {
            "kitsune_agent_id": ctx.agent_id,
            "kitsune_run_id": str(ctx.run_id),
            "kitsune_correlation_id": str(ctx.correlation_id),
        },
        "tags": ["kitsune", f"kitsune-agent:{ctx.agent_id}"],
    }
    return merge_configs(config, instrumentation)


def _find_usage(response: Any) -> Mapping[str, Any]:
    llm_output = _mapping_or_none(getattr(response, "llm_output", None))
    if llm_output is not None:
        for key in ("token_usage", "usage", "usage_metadata"):
            candidate = _mapping_or_none(llm_output.get(key))
            if candidate is not None:
                return candidate
    raw_generations = getattr(response, "generations", None)
    if isinstance(raw_generations, Sequence) and not isinstance(raw_generations, str | bytes):
        generations = cast(Sequence[object], raw_generations)
        for raw_group in generations:
            if not isinstance(raw_group, Sequence) or isinstance(raw_group, str | bytes):
                continue
            group = cast(Sequence[object], raw_group)
            for generation in group:
                message = getattr(generation, "message", None)
                candidate = _mapping_or_none(getattr(message, "usage_metadata", None))
                if candidate is not None:
                    return candidate
    response_mapping = _mapping_or_none(response)
    if response_mapping is not None:
        for key in ("usage_metadata", "token_usage", "usage"):
            candidate = _mapping_or_none(response_mapping.get(key))
            if candidate is not None:
                return candidate
    return {}


def _mapping_or_none(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    return cast(dict[str, Any], {str(key): item for key, item in mapping.items()})


def _mapping_int(value: object, *names: str) -> int | None:
    mapping = _mapping_or_none(value)
    if mapping is None:
        return None
    for name in names:
        item = mapping.get(name)
        if item is not None:
            return int(item)
    return None


def _nested_string(value: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        item = value.get(name)
        if item is not None:
            return str(item)
    return None


def _has_known_usage(record: UsageRecord) -> bool:
    return any(
        value is not None
        for value in (
            record.request_count,
            record.input_tokens,
            record.output_tokens,
            record.total_tokens,
            record.cache_read_tokens,
            record.cache_write_tokens,
        )
    )
