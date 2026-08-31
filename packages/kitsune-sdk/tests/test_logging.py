"""Structured SDK log redaction regressions."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, SecretStr

from kitsune import (
    EventOutbox,
    KitsuneApp,
    KitsuneSettings,
    PluginMetadata,
    RunContext,
    Telemetry,
)
from kitsune.logging import JsonFormatter, redact_sensitive_data


class Request(BaseModel):
    value: str


class Result(BaseModel):
    value: str


def test_json_formatter_redacts_message_details_exception_and_stack() -> None:
    try:
        error = RuntimeError("token=sdk-exception-secret")
        error.add_note("password=note-secret")
        raise error
    except RuntimeError:
        record = logging.LogRecord(
            "agent",
            logging.ERROR,
            __file__,
            1,
            "request failed authorization: Bearer message-secret",
            (),
            sys.exc_info(),
        )
    record.details = {
        "password": "detail-secret",
        "region": "ap-northeast-1",
        "nested": "api-key=nested-secret",
        "note": "standalone-bare-secret",
    }
    record.stack_info = "Stack\nauthorization: Basic stack-secret"

    rendered = JsonFormatter(
        service="agent",
        redacted_keys=frozenset({"authorization", "api_key", "password", "token"}),
        redacted_values=frozenset({"standalone-bare-secret"}),
    ).format(record)
    payload = json.loads(rendered)

    for secret in (
        "sdk-exception-secret",
        "note-secret",
        "message-secret",
        "detail-secret",
        "nested-secret",
        "stack-secret",
        "standalone-bare-secret",
    ):
        assert secret not in rendered
    assert payload["details"]["password"] == "[REDACTED]"
    assert payload["details"]["region"] == "ap-northeast-1"
    assert "RuntimeError" in payload["exception"]
    assert "[REDACTED]" in payload["exception"]
    assert "[REDACTED]" in payload["stack"]


def test_sdk_redaction_matches_snake_camel_and_header_sensitive_components() -> None:
    """Empty additions retain the baseline across mapping and text key spellings."""

    keys = KitsuneSettings(redacted_keys=frozenset()).redacted_keys
    record = logging.LogRecord(
        "agent",
        logging.ERROR,
        __file__,
        1,
        (
            "access_token=snake-text-secret refreshToken=camel-text-secret "
            "privateKey=private-text-secret X-API-Key: header-text-secret "
            'api.key=dotted-text-secret "private.key":"quoted-dotted-secret"'
        ),
        (),
        None,
    )
    details = {
        "access_token": "snake-mapping-secret",
        "refreshToken": "camel-mapping-secret",
        "private_key": "private-mapping-secret",
        "X-API-Key": "header-mapping-secret",
        "safe_monkey": "visible",
    }
    record.details = details

    rendered = JsonFormatter(service="agent", redacted_keys=keys).format(record)
    payload = json.loads(rendered)
    sanitized_event = redact_sensitive_data(details, keys)
    configured_keys = KitsuneSettings(redacted_keys=frozenset({"tenantSession"})).redacted_keys
    configured_addition = redact_sensitive_data(
        {"X-Tenant-Session": "configured-addition-secret"}, configured_keys
    )

    for secret in (
        "snake-text-secret",
        "camel-text-secret",
        "private-text-secret",
        "header-text-secret",
        "dotted-text-secret",
        "quoted-dotted-secret",
        "snake-mapping-secret",
        "camel-mapping-secret",
        "private-mapping-secret",
        "header-mapping-secret",
    ):
        assert secret not in rendered
        assert secret not in repr(sanitized_event)
    assert payload["details"] == {
        "access_token": "[REDACTED]",
        "refreshToken": "[REDACTED]",
        "private_key": "[REDACTED]",
        "X-API-Key": "[REDACTED]",
        "safe_monkey": "visible",
    }
    assert sanitized_event == payload["details"]
    assert configured_addition == {"X-Tenant-Session": "[REDACTED]"}


@pytest.mark.asyncio
async def test_handler_exception_text_never_enters_spans_or_durable_outbox(tmp_path: Path) -> None:
    secret = "audit-literal-secret"
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry.create("secure-agent", owned_tracer_provider=provider)
    settings = KitsuneSettings(
        outbox_path=tmp_path / "events.sqlite3",
        agent_token=SecretStr(secret),
    )
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="secure-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
        telemetry=telemetry,
    )

    @application.handler("fail", input_model=Request, output_model=Result)
    async def fail(_: RunContext, request: Request) -> Result:
        raise RuntimeError(f"Authorization: Bearer {secret}; value={request.value}")

    await application.startup()
    try:
        await application.emit(
            "kitsune.custom.secret",
            payload={"note": f"unkeyed value {secret} must not persist"},
        )
        with pytest.raises(RuntimeError, match=secret):
            await application.execute("fail", {"value": "ordinary"})
        pending = await outbox.pending(limit=20)
    finally:
        await application.shutdown()

    serialized_events = json.dumps(
        [item.event.model_dump(mode="json") for item in pending],
        sort_keys=True,
    )
    spans = [
        {
            "name": span.name,
            "status": span.status.description,
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
        }
        for span in exporter.get_finished_spans()
    ]
    serialized_spans = json.dumps(spans, sort_keys=True)

    assert secret not in serialized_events
    assert "Authorization" not in serialized_events
    failed = next(item.event for item in pending if item.event.type == "kitsune.run.failed")
    assert failed.payload["error"] == {
        "type": "RuntimeError",
        "message": "Handler execution failed",
        "retryable": False,
        "details": {},
    }
    assert secret not in serialized_spans
    assert "Authorization" not in serialized_spans
    run_span = next(span for span in spans if span["name"] == "kitsune.run")
    assert run_span["events"] == [
        {"name": "exception", "attributes": {"exception.type": "RuntimeError"}}
    ]


@pytest.mark.asyncio
async def test_startup_exception_span_exports_only_type(tmp_path: Path) -> None:
    secret = "startup-span-sentinel"
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry.create("startup-agent", owned_tracer_provider=provider)
    application = KitsuneApp(
        agent_id="startup-agent",
        version="1.0.0",
        settings=KitsuneSettings(outbox_path=tmp_path / "startup.sqlite3"),
        telemetry=telemetry,
    )

    class FailingStartupPlugin:
        metadata = PluginMetadata(name="startup-failure", version="1.0.0", critical=True)

        def configure(self, app: Any) -> None:
            del app

        async def start(self, app: Any) -> None:
            del app
            raise RuntimeError(f"Authorization: Bearer {secret}")

        async def stop(self, app: Any) -> None:
            del app

    application.use(FailingStartupPlugin())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match=secret):
        await application.startup()

    span = next(
        item for item in exporter.get_finished_spans() if item.name == "kitsune.agent.start"
    )
    serialized = repr(
        {
            "status": span.status.description,
            "events": [dict(event.attributes or {}) for event in span.events],
        }
    )
    assert secret not in serialized
    assert span.events[0].attributes == {"exception.type": "RuntimeError"}


@pytest.mark.asyncio
async def test_child_run_exception_is_generic_in_outcome_outbox_and_span(tmp_path: Path) -> None:
    secret = "child-run-sentinel"
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry.create("child-agent", owned_tracer_provider=provider)
    settings = KitsuneSettings(outbox_path=tmp_path / "child.sqlite3")
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="child-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
        telemetry=telemetry,
    )
    outcomes: list[Any] = []

    class OutcomePlugin:
        metadata = PluginMetadata(name="outcomes", version="1.0.0")

        def configure(self, app: Any) -> None:
            del app

        async def start(self, app: Any) -> None:
            del app

        async def stop(self, app: Any) -> None:
            del app

        async def on_run_started(self, ctx: RunContext) -> None:
            del ctx

        async def on_run_finished(self, ctx: RunContext, outcome: Any) -> None:
            del ctx
            outcomes.append(outcome)

        async def on_event(self, event: Any) -> None:
            del event

    application.use(OutcomePlugin())  # type: ignore[arg-type]

    @application.handler("parent", input_model=Request, output_model=Result)
    async def parent(ctx: RunContext, request: Request) -> Result:
        async with ctx.child_run(name="secret-child"):
            raise RuntimeError(f"Authorization: Bearer {secret}")

    await application.startup()
    try:
        with pytest.raises(RuntimeError, match=secret):
            await application.execute("parent", {"value": "ordinary"})
        pending = await outbox.pending(limit=20)
    finally:
        await application.shutdown()

    serialized = repr(
        {
            "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
            "events": [item.event.model_dump(mode="json") for item in pending],
            "spans": [
                {
                    "status": span.status.description,
                    "events": [dict(event.attributes or {}) for event in span.events],
                }
                for span in exporter.get_finished_spans()
            ],
        }
    )
    assert secret not in serialized
    child_outcome = next(outcome for outcome in outcomes if outcome.error is not None)
    assert child_outcome.error.message == "Child Run failed"
    child_span = next(
        span for span in exporter.get_finished_spans() if span.name == "kitsune.child_run"
    )
    assert child_span.events[0].attributes == {"exception.type": "RuntimeError"}


@pytest.mark.asyncio
async def test_redacted_environment_value_never_enters_sdk_stdout_or_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The official SDK applies Workspace/Operator environment names at both boundaries."""

    secret = "bare-provider-secret"
    monkeypatch.setenv("OPAQUE_PROVIDER_VALUE", secret)
    monkeypatch.setenv(
        "KITSUNE_REDACTED_ENVIRONMENT_VARIABLES",
        '["OPAQUE_PROVIDER_VALUE"]',
    )
    settings = KitsuneSettings(
        service_name="environment-redaction-agent",
        outbox_path=tmp_path / "environment.sqlite3",
    )
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="environment-redaction-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
    )
    output = io.StringIO()
    handler = next(
        item for item in application.logger.handlers if isinstance(item.formatter, JsonFormatter)
    )
    assert isinstance(handler, logging.StreamHandler)
    original_stream = handler.setStream(output)

    @application.handler("provider", input_model=Request, output_model=Result)
    async def provider(ctx: RunContext, request: Request) -> Result:
        ctx.logger.error(f"provider returned bare credential {secret}")
        await ctx.emit("kitsune.provider.failed", payload={"note": f"bare {secret}"})
        return Result(value=request.value)

    try:
        await application.startup()
        await application.execute("provider", {"value": "ordinary"})
        pending = await outbox.pending(limit=20)
        await application.shutdown()
    finally:
        handler.setStream(original_stream)

    serialized_outbox = json.dumps(
        [item.event.model_dump(mode="json") for item in pending], sort_keys=True
    )
    assert secret not in output.getvalue()
    assert secret not in serialized_outbox
    assert "[REDACTED]" in output.getvalue()
    assert "[REDACTED]" in serialized_outbox


@pytest.mark.asyncio
async def test_secret_like_environment_values_are_redacted_without_explicit_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone SDK use auto-discovers standard secret-bearing environment names."""

    secrets = {
        "OPENAI_API_KEY": "bare-openai-secret",
        "AWS_SESSION_TOKEN": "bare-aws-secret",
        "DATABASE_PASSWORD": "bare-database-secret",
        "CUSTOM_CREDENTIAL": "bare-custom-secret",
    }
    monkeypatch.delenv("KITSUNE_REDACTED_ENVIRONMENT_VARIABLES", raising=False)
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    settings = KitsuneSettings(
        service_name="automatic-environment-redaction-agent",
        outbox_path=tmp_path / "automatic-environment.sqlite3",
        redacted_keys=frozenset({"credential"}),
    )
    assert not settings.redacted_environment_variables
    outbox = EventOutbox(settings.outbox_path)
    application = KitsuneApp(
        agent_id="automatic-environment-redaction-agent",
        version="1.0.0",
        settings=settings,
        outbox=outbox,
    )
    output = io.StringIO()
    handler = next(
        item for item in application.logger.handlers if isinstance(item.formatter, JsonFormatter)
    )
    assert isinstance(handler, logging.StreamHandler)
    original_stream = handler.setStream(output)
    bare_values = " ".join(secrets.values())

    @application.handler("provider", input_model=Request, output_model=Result)
    async def provider(ctx: RunContext, request: Request) -> Result:
        ctx.logger.error(f"provider returned credentials {bare_values}")
        await ctx.emit("kitsune.provider.failed", payload={"note": bare_values})
        return Result(value=request.value)

    try:
        await application.startup()
        await application.execute("provider", {"value": "ordinary"})
        pending = await outbox.pending(limit=20)
        await application.shutdown()
    finally:
        handler.setStream(original_stream)

    serialized_outbox = json.dumps(
        [item.event.model_dump(mode="json") for item in pending], sort_keys=True
    )
    for secret in secrets.values():
        assert secret not in output.getvalue()
        assert secret not in serialized_outbox
    assert "[REDACTED]" in output.getvalue()
    assert "[REDACTED]" in serialized_outbox
