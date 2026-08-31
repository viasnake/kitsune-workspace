"""Langfuse enablement, masking, Context, flush, and shutdown tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from kitsune import KitsuneApp, KitsuneEvent, KitsuneSettings, RunContext
from pydantic import BaseModel, SecretStr

from kitsune_plugin_langfuse import (
    LANGFUSE_EXTENSION,
    LangfuseContextState,
    LangfusePlugin,
    LangfuseSettings,
    mask_payload,
)


class Request(BaseModel):
    """Langfuse test input."""

    value: str


class Result(BaseModel):
    """Langfuse test output."""

    enabled: bool


class FakeLangfuse:
    """Record constructor masking and shutdown operations without network I/O."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.flushed = False
        self.stopped = False

    def flush(self) -> None:
        self.flushed = True

    async def shutdown(self) -> None:
        self.stopped = True


def make_app(tmp_path: Path) -> KitsuneApp:
    """Build an isolated Langfuse Plugin application."""

    return KitsuneApp(
        agent_id="langfuse-agent",
        version="1.0.0",
        settings=KitsuneSettings(outbox_path=tmp_path / "events.sqlite3"),
    )


def test_mask_payload_redacts_nested_sensitive_keys() -> None:
    """Configured keys are redacted recursively without mutating nonsensitive data."""

    payload = {"message": "ok", "nested": {"Authorization": "Bearer secret"}}

    masked = mask_payload(payload, frozenset({"authorization"}))

    assert masked == {"message": "ok", "nested": {"Authorization": "[REDACTED]"}}


@pytest.mark.asyncio
async def test_event_attributes_use_shared_compound_key_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event span attributes cannot bypass masking with separator or camel variants."""

    captured: list[dict[str, Any]] = []

    class CapturingSpan:
        def add_event(self, name: str, attributes: dict[str, Any]) -> None:
            captured.append({"name": name, "attributes": attributes})

    monkeypatch.setattr(
        "kitsune_plugin_langfuse.plugin.trace.get_current_span",
        lambda: CapturingSpan(),
    )
    plugin = LangfusePlugin(LangfuseSettings(redacted_keys=frozenset()))
    event = KitsuneEvent(
        type="kitsune.security.redaction",
        occurred_at=datetime.now(UTC),
        agent_id="langfuse-agent",
        payload={
            "access_token": "snake-secret",
            "refreshToken": "camel-secret",
            "private-key": "private-secret",
            "x-api-key": "header-secret",
            "assignment_note": "access_token=scalar-event-secret",
            "dotted_note": "api.key=dotted-event-secret",
            "header_note": "Authorization: Bearer scalar-header-secret",
            "safe_monkey": "visible",
        },
    )

    await plugin.on_event(event)

    assert captured == [
        {
            "name": "kitsune.security.redaction",
            "attributes": {
                "kitsune.event.access_token": "[REDACTED]",
                "kitsune.event.refreshToken": "[REDACTED]",
                "kitsune.event.private-key": "[REDACTED]",
                "kitsune.event.x-api-key": "[REDACTED]",
                "kitsune.event.assignment_note": "access_token=[REDACTED]",
                "kitsune.event.dotted_note": "api.key=[REDACTED]",
                "kitsune.event.header_note": "Authorization: [REDACTED]",
                "kitsune.event.safe_monkey": "visible",
            },
        }
    ]


@pytest.mark.asyncio
async def test_disabled_plugin_never_constructs_langfuse_client(tmp_path: Path) -> None:
    """Disabled Langfuse preserves the same Agent code without client initialization."""

    called = False

    def forbidden_factory(**kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("disabled Langfuse must not construct a client")

    app = make_app(tmp_path)
    app.use(LangfusePlugin(LangfuseSettings(enabled=False), client_factory=forbidden_factory))

    @app.handler("state", input_model=Request, output_model=Result)
    async def state(ctx: RunContext, request: Request) -> Result:
        extension = ctx.extension(LANGFUSE_EXTENSION)
        assert isinstance(extension, LangfuseContextState)
        return Result(enabled=extension.enabled)

    async with app:
        result = Result.model_validate(await app.execute("state", {"value": "same-code"}))

    assert not result.enabled
    assert not called


@pytest.mark.asyncio
async def test_enabled_plugin_passes_mask_and_flushes_client(tmp_path: Path) -> None:
    """Enabled Langfuse receives masking configuration and is flushed on shutdown."""

    clients: list[FakeLangfuse] = []

    def factory(**kwargs: Any) -> FakeLangfuse:
        client = FakeLangfuse(**kwargs)
        clients.append(client)
        return client

    plugin = LangfusePlugin(
        LangfuseSettings(
            enabled=True,
            public_key=SecretStr("public"),
            secret_key=SecretStr("secret"),
        ),
        client_factory=factory,
    )
    app = make_app(tmp_path)
    app.use(plugin)

    @app.handler("state", input_model=Request, output_model=Result)
    async def state(ctx: RunContext, request: Request) -> Result:
        return Result(enabled=True)

    async with app:
        await app.execute("state", {"value": "captured"})

    client = clients[0]
    mask = client.kwargs["mask"]
    assert callable(mask)
    assert mask(data="ordinary prompt") == "[REDACTED]"
    assert mask(data={"result": "ordinary output"}) == "[REDACTED]"
    assert client.kwargs["secret_key"] == "secret"
    assert client.flushed and client.stopped


@pytest.mark.asyncio
async def test_disabled_io_mask_keeps_only_key_redaction_in_native_callback(
    tmp_path: Path,
) -> None:
    """Disabling full I/O masking still redacts configured sensitive keys."""

    clients: list[FakeLangfuse] = []

    def factory(**kwargs: Any) -> FakeLangfuse:
        client = FakeLangfuse(**kwargs)
        clients.append(client)
        return client

    app = make_app(tmp_path)
    app.use(
        LangfusePlugin(
            LangfuseSettings(
                enabled=True,
                mask_io=False,
                redacted_keys=frozenset({"tenantSession"}),
            ),
            client_factory=factory,
        )
    )

    @app.handler("state", input_model=Request, output_model=Result)
    async def state(ctx: RunContext, request: Request) -> Result:
        return Result(enabled=True)

    async with app:
        await app.execute("state", {"value": "captured"})

    mask = clients[0].kwargs["mask"]
    assert mask(
        data={
            "message": "visible",
            "access_token": "snake-secret",
            "refreshToken": "camel-secret",
            "private-key": "private-secret",
            "x-api-key": "header-secret",
            "X-Tenant-Session": "configured-secret",
        }
    ) == {
        "message": "visible",
        "access_token": "[REDACTED]",
        "refreshToken": "[REDACTED]",
        "private-key": "[REDACTED]",
        "x-api-key": "[REDACTED]",
        "X-Tenant-Session": "[REDACTED]",
    }
    assert mask(data="access_token=scalar-native-secret") == "access_token=[REDACTED]"
    assert mask(data="Authorization: Bearer scalar-native-header") == ("Authorization: [REDACTED]")
