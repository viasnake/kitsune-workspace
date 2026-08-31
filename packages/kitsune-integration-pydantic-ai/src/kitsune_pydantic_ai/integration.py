"""Pydantic AI integration that preserves its native Agent and Model abstractions."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from kitsune import ModelCallAdmission, RunContext
from kitsune_contracts import UsageRecord
from opentelemetry.context import Context
from opentelemetry.trace import (
    Link,
    Span,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    TracerProvider,
    get_tracer_provider,
)
from opentelemetry.util._decorator import _AgnosticContextManager
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse, infer_model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.instrumented import InstrumentationSettings, InstrumentedModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

ModelReference = str | Model
_active_run: ContextVar[_RunInstrumentation | None]


def _record_safe_exception(span: Span, exception: BaseException) -> None:
    span.add_event("exception", {"exception.type": type(exception).__name__})
    span.set_status(Status(StatusCode.ERROR))


class _ExceptionSafeTracer(Tracer):
    """Delegate native spans while disabling OpenTelemetry's raw exception capture."""

    def __init__(self, delegate: Tracer) -> None:
        self._delegate = delegate

    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, AttributeValue] | None = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> _AgnosticContextManager[Span]:
        return self._delegate.start_as_current_span(
            name,
            context=context,
            kind=kind,
            attributes=attributes,
            links=links,
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
            end_on_exit=end_on_exit,
        )

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, AttributeValue] | None = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        return self._delegate.start_span(
            name,
            context=context,
            kind=kind,
            attributes=attributes,
            links=links,
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
        )


class _ExceptionSafeTracerProvider(TracerProvider):
    def __init__(self, delegate: TracerProvider) -> None:
        self._delegate = delegate

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> Tracer:
        return _ExceptionSafeTracer(
            self._delegate.get_tracer(
                instrumenting_module_name,
                instrumenting_library_version,
                schema_url,
                attributes,
            )
        )


@dataclass(slots=True)
class _RunInstrumentation:
    context: RunContext
    finalization: bool
    first_admission: ModelCallAdmission | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def admit_request(self) -> ModelCallAdmission:
        async with self.lock:
            if self.first_admission is not None:
                admission = self.first_admission
                self.first_admission = None
                return admission
            return await self.context.check_model_call(finalization=self.finalization)


_active_run = ContextVar("kitsune_pydantic_ai_active_run", default=None)


class KitsuneModel(WrapperModel):
    """Native Pydantic AI Model wrapper that guards every provider request."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Check Budget before one request and record exact response Usage."""

        active = _active_run.get()
        admission = await active.admit_request() if active is not None else None
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
            if active is not None:
                await active.context.record_usage(
                    usage_to_record(
                        response.usage,
                        provider=response.provider_name,
                        model=response.model_name,
                    )
                )
            return response
        except BaseException:
            if admission is not None:
                admission.release()
            raise

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """Apply the per-request guard and Usage capture to streaming Models."""

        active = _active_run.get()
        admission = await active.admit_request() if active is not None else None
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response
                if active is not None:
                    completed = response.get()
                    await active.context.record_usage(
                        usage_to_record(
                            completed.usage,
                            provider=completed.provider_name,
                            model=completed.model_name,
                        )
                    )
        except BaseException:
            if admission is not None:
                admission.release()
            raise


class PydanticAIModelConfiguration(BaseModel):
    """Provider-neutral Pydantic AI primary and fallback Model references."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    primary: ModelReference
    fallbacks: tuple[ModelReference, ...] = ()

    def resolve(self) -> Model:
        """Resolve native Pydantic AI Models without wrapping provider SDKs."""

        return resolve_model(self.primary, self.fallbacks)


class ProviderModelConfiguration(BaseModel):
    """OpenAI or Anthropic Model identifier without a Kitsune-owned Model catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai", "anthropic"]
    model: str = Field(min_length=1)

    @property
    def identifier(self) -> str:
        """Return the Pydantic AI ``provider:model`` inference string."""

        return f"{self.provider}:{self.model}"


class MCPServerConfiguration(BaseModel):
    """Configuration translated directly to a native Pydantic AI MCP Server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["stdio", "streamable_http", "sse"]
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    cwd: Path | None = None
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> MCPServerConfiguration:
        """Require command for stdio and URL for network transports."""

        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP configuration requires command")
        if self.transport != "stdio" and not self.url:
            raise ValueError(f"{self.transport} MCP configuration requires url")
        return self

    def build(self) -> MCPToolset:
        """Build the matching native Pydantic AI MCP toolset."""

        from fastmcp.client.transports.base import ClientTransport
        from fastmcp.client.transports.http import StreamableHttpTransport
        from fastmcp.client.transports.sse import SSETransport
        from fastmcp.client.transports.stdio import StdioTransport

        transport: ClientTransport
        if self.transport == "stdio":
            transport = StdioTransport(
                command=self.command or "",
                args=list(self.args),
                env=dict(self.env) if self.env else None,
                cwd=str(self.cwd) if self.cwd else None,
            )
        elif self.transport == "streamable_http":
            transport = StreamableHttpTransport(
                url=self.url or "",
                headers=dict(self.headers) if self.headers else None,
                sse_read_timeout=self.timeout_seconds,
            )
        else:
            transport = SSETransport(
                url=self.url or "",
                headers=dict(self.headers) if self.headers else None,
                sse_read_timeout=self.timeout_seconds,
            )
        return MCPToolset(
            transport,
            init_timeout=self.timeout_seconds,
            read_timeout=self.timeout_seconds,
        )


def resolve_model(
    primary: ModelReference,
    fallbacks: Sequence[ModelReference] = (),
) -> Model:
    """Resolve a primary Pydantic AI Model and ordered native fallbacks."""

    models = [
        _instrument_model(_resolve_one(primary)),
        *(_instrument_model(_resolve_one(model)) for model in fallbacks),
    ]
    return models[0] if len(models) == 1 else FallbackModel(*models)


def _resolve_one(reference: ModelReference) -> Model:
    return infer_model(reference) if isinstance(reference, str) else reference


def _instrument_model(model: Model) -> Model:
    return _rewrite_model_graph(model, memo={}, active=set())


def _rewrite_model_graph(
    model: Model,
    *,
    memo: dict[int, Model],
    active: set[int],
) -> Model:
    """Strip nested native instrumentation and guard every provider-attempt leaf."""

    identity = id(model)
    if identity in active:
        raise ValueError("Pydantic AI Model wrapper graph contains a cycle")
    if identity in memo:
        return memo[identity]

    active.add(identity)
    try:
        if isinstance(model, InstrumentedModel | KitsuneModel):
            rewritten = _rewrite_model_graph(model.wrapped, memo=memo, active=active)
        elif isinstance(model, FallbackModel):
            if type(model) is not FallbackModel:
                raise ValueError(
                    f"unsupported Pydantic AI FallbackModel subclass: {type(model).__name__}"
                )
            rewritten_models = [
                _rewrite_model_graph(item, memo=memo, active=active) for item in model.models
            ]
            rewritten = copy.copy(model)
            try:
                rewritten.models = rewritten_models
            except (AttributeError, TypeError) as exc:
                raise ValueError("Pydantic AI FallbackModel cannot be safely rewritten") from exc
            if len(rewritten.models) != len(rewritten_models) or any(
                actual is not expected
                for actual, expected in zip(rewritten.models, rewritten_models, strict=True)
            ):
                raise ValueError("Pydantic AI FallbackModel rejected its safe Model graph")
        elif isinstance(model, WrapperModel):
            rewritten_inner = _rewrite_model_graph(model.wrapped, memo=memo, active=active)
            if type(model) is WrapperModel:
                rewritten = WrapperModel(rewritten_inner)
            elif type(model) is ConcurrencyLimitedModel:
                rewritten = ConcurrencyLimitedModel(
                    rewritten_inner,
                    model._limiter,
                )
            else:
                raise ValueError(f"unsupported Pydantic AI Model wrapper: {type(model).__name__}")
            if rewritten.wrapped is not rewritten_inner:
                raise ValueError(
                    f"Pydantic AI {type(model).__name__} rejected its safe wrapped Model"
                )
        else:
            rewritten = KitsuneModel(model)
    finally:
        active.remove(identity)

    memo[identity] = rewritten
    return rewritten


def _safe_instrumentation_settings() -> InstrumentationSettings:
    return InstrumentationSettings(
        tracer_provider=_ExceptionSafeTracerProvider(get_tracer_provider()),
        include_content=False,
        include_binary_content=False,
    )


def _contains_instrumentation_capability(capability: Any) -> bool:
    if isinstance(capability, Instrumentation):
        return True
    apply = getattr(capability, "apply", None)
    if not callable(apply):
        return False
    found = False

    def inspect(item: Any) -> None:
        nonlocal found
        found = found or isinstance(item, Instrumentation)

    apply(inspect)
    return found


def _is_instrumentation_spec(value: Any) -> bool:
    if getattr(value, "name", None) == "Instrumentation":
        return True
    if isinstance(value, str):
        return value == "Instrumentation"
    return isinstance(value, Mapping) and "Instrumentation" in value


def _reject_explicit_instrumentation_capabilities(
    agent: Any,
    run_kwargs: Mapping[str, Any],
) -> None:
    capabilities = [getattr(agent, "_root_capability", None)]
    override_context = getattr(agent, "_override_root_capability", None)
    if override_context is not None:
        get_override = getattr(override_context, "get", None)
        if callable(get_override):
            capabilities.append(getattr(get_override(), "value", None))
    capabilities.extend(run_kwargs.get("capabilities") or ())
    if any(
        capability is not None and _contains_instrumentation_capability(capability)
        for capability in capabilities
    ):
        raise ValueError(
            "Kitsune run_agent does not allow explicit Pydantic AI Instrumentation "
            "capabilities; Kitsune provides exception-safe native instrumentation"
        )

    spec = run_kwargs.get("spec")
    spec_capabilities = (
        spec.get("capabilities", ())
        if isinstance(spec, Mapping)
        else getattr(spec, "capabilities", ())
    )
    if any(_is_instrumentation_spec(item) for item in spec_capabilities or ()):
        raise ValueError(
            "Kitsune run_agent does not allow explicit Pydantic AI Instrumentation "
            "capabilities; Kitsune provides exception-safe native instrumentation"
        )


def usage_to_record(
    usage: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> UsageRecord:
    """Convert available Pydantic AI usage fields without estimating missing values."""

    raw_details = getattr(usage, "details", None)
    details: Mapping[str, Any] = (
        cast(Mapping[str, Any], raw_details) if isinstance(raw_details, Mapping) else {}
    )
    requests = _optional_int(usage, "requests", "request_count")
    input_tokens = _optional_int(usage, "input_tokens")
    output_tokens = _optional_int(usage, "output_tokens")
    total_tokens = _optional_int(usage, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return UsageRecord(
        provider=provider,
        model=model,
        request_count=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=_first_known(
            _optional_int(usage, "cache_read_tokens"),
            _mapping_int(details, "cache_read_tokens", "cached_tokens", "cache_read"),
        ),
        cache_write_tokens=_first_known(
            _optional_int(usage, "cache_write_tokens"),
            _mapping_int(details, "cache_write_tokens", "cache_write"),
        ),
    )


async def run_agent(
    agent: Any,
    prompt: Any,
    *,
    ctx: RunContext,
    finalization: bool = False,
    **run_kwargs: Any,
) -> Any:
    """Run a native Pydantic AI Agent under Kitsune trace, budget, and usage context."""

    instrument_pydantic_ai()
    with ctx.tracer.start_as_current_span(
        "kitsune.pydantic_ai.run",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("kitsune.agent.id", ctx.agent_id)
        span.set_attribute("kitsune.run.id", str(ctx.run_id))
        try:
            _reject_explicit_instrumentation_capabilities(agent, run_kwargs)
            model = run_kwargs.pop("model", None) or getattr(agent, "model", None)
            if model is None:
                raise ValueError("Pydantic AI Agent has no configured Model")
            guarded_model = InstrumentedModel(
                _instrument_model(_resolve_one(model)),
                _safe_instrumentation_settings(),
            )
            initial_admission = await ctx.check_model_call(finalization=finalization)
            token: Token[_RunInstrumentation | None] = _active_run.set(
                _RunInstrumentation(
                    context=ctx,
                    finalization=finalization,
                    first_admission=initial_admission,
                )
            )
            try:
                return await agent.run(prompt, model=guarded_model, **run_kwargs)
            finally:
                initial_admission.release()
                _active_run.reset(token)
        except BaseException as exc:
            _record_safe_exception(span, exc)
            raise


def create_test_model(*, custom_output_text: str | None = None) -> TestModel:
    """Create Pydantic AI's native deterministic TestModel for offline tests."""

    return TestModel(custom_output_text=custom_output_text)


def instrument_pydantic_ai() -> None:
    """Enable native Model and Tool spans without capturing prompt or result content."""

    from pydantic_ai import Agent

    Agent.instrument_all(_safe_instrumentation_settings())


def _optional_int(value: Any, *names: str) -> int | None:
    for name in names:
        item = getattr(value, name, None)
        if item is not None:
            return int(item)
    return None


def _mapping_int(value: Mapping[Any, Any], *names: str) -> int | None:
    for name in names:
        item = value.get(name)
        if item is not None:
            return int(item)
    return None


def _first_known(*values: int | None) -> int | None:
    return next((value for value in values if value is not None), None)
