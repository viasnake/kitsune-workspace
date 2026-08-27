"""Optional Langfuse 3 OpenTelemetry Plugin with deterministic masking."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from kitsune import AppBuilder, KitsuneEvent, PluginMetadata, RunContext, RunningApp, RunOutcome
from kitsune.logging import (
    MANDATORY_REDACTED_KEYS,
    normalize_redacted_keys,
    redact_sensitive_data,
)
from opentelemetry import trace
from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LANGFUSE_EXTENSION = "kitsune.langfuse"


class LangfuseSettings(BaseSettings):
    """Langfuse connection and data masking settings."""

    model_config = SettingsConfigDict(
        env_prefix="LANGFUSE_", env_file=None, extra="ignore", case_sensitive=False
    )

    enabled: bool = True
    public_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    base_url: AnyHttpUrl | None = None
    environment: str | None = None
    release: str | None = None
    mask_io: bool = True
    redacted_keys: frozenset[str] = Field(default=MANDATORY_REDACTED_KEYS)

    @field_validator("redacted_keys")
    @classmethod
    def retain_mandatory_redactions(cls, value: frozenset[str]) -> frozenset[str]:
        """Treat configured keys as additions to the shared non-disableable baseline."""

        return normalize_redacted_keys(value)


@dataclass(frozen=True, slots=True)
class LangfuseContextState:
    """Read-only Langfuse enablement state exposed on each Run Context."""

    enabled: bool


class LangfusePlugin:
    """Connect Langfuse's OTel-based SDK without making it part of Kitsune Core."""

    def __init__(
        self,
        settings: LangfuseSettings | None = None,
        *,
        client_factory: Any | None = None,
        critical: bool = False,
    ) -> None:
        self.settings = settings or LangfuseSettings()
        self.metadata = PluginMetadata(name="kitsune-langfuse", version="1.0.0", critical=critical)
        self._client_factory = client_factory
        self._client: Any | None = None
        self._events_observed = 0

    def configure(self, app: AppBuilder) -> None:
        """Expose immutable Langfuse enablement on each Run Context."""

        app.add_context_extension(
            LANGFUSE_EXTENSION,
            lambda _ctx: LangfuseContextState(enabled=self.settings.enabled),
        )

    async def start(self, app: RunningApp) -> None:
        """Initialize Langfuse only when explicitly enabled."""

        if not self.settings.enabled:
            await app.emit("kitsune.langfuse.disabled")
            return
        factory = self._client_factory
        if factory is None:
            from langfuse import Langfuse

            factory = Langfuse
        kwargs: dict[str, Any] = {"mask": self._mask_langfuse_data}
        if self.settings.public_key is not None:
            kwargs["public_key"] = self.settings.public_key.get_secret_value()
        if self.settings.secret_key is not None:
            kwargs["secret_key"] = self.settings.secret_key.get_secret_value()
        if self.settings.base_url is not None:
            kwargs["base_url"] = str(self.settings.base_url)
        if self.settings.environment is not None:
            kwargs["environment"] = self.settings.environment
        if self.settings.release is not None:
            kwargs["release"] = self.settings.release
        self._client = factory(**kwargs)
        await app.emit("kitsune.langfuse.enabled")

    async def stop(self, app: RunningApp) -> None:
        """Flush and shut down the optional Langfuse Client."""

        if self._client is not None:
            await _call_optional(self._client, "flush")
            await _call_optional(self._client, "shutdown")
            self._client = None
        await app.emit(
            "kitsune.langfuse.stopped", payload={"events_observed": self._events_observed}
        )

    async def on_run_started(self, ctx: RunContext) -> None:
        """Correlate the active OTel Span with Kitsune Run identity."""

        span = trace.get_current_span()
        span.set_attribute("kitsune.run.id", str(ctx.run_id))
        span.set_attribute("kitsune.run.correlation_id", str(ctx.correlation_id))
        span.set_attribute("langfuse.trace.name", f"kitsune:{ctx.agent_id}")

    async def on_run_finished(self, ctx: RunContext, outcome: RunOutcome) -> None:
        """Record the terminal Kitsune Run status on the active OTel Span."""

        span = trace.get_current_span()
        span.set_attribute("kitsune.run.status", outcome.status.value)
        span.set_attribute("kitsune.run.usage_records", len(outcome.usage))

    async def on_event(self, event: KitsuneEvent) -> None:
        """Attach sanitized Kitsune Events to the current OTel Span."""

        self._events_observed += 1
        masked_payload = mask_payload(event.payload, self.settings.redacted_keys)
        attributes = {
            f"kitsune.event.{key}": value
            for key, value in masked_payload.items()
            if isinstance(value, str | bool | int | float)
        }
        trace.get_current_span().add_event(event.type, attributes=attributes)

    def _mask_langfuse_data(self, *, data: Any, **_: Any) -> Any:
        """Apply the native callback's single policy to unclassified input or output data."""

        if self.settings.mask_io:
            return "[REDACTED]"
        return mask_payload(data, self.settings.redacted_keys)


def mask_payload(value: Any, redacted_keys: frozenset[str]) -> Any:
    """Apply the SDK's shared mandatory and configured sensitive-key policy."""

    normalized = normalize_redacted_keys(redacted_keys)
    return redact_sensitive_data(value, normalized)


async def _call_optional(client: Any, method_name: str) -> None:
    method = getattr(client, method_name, None)
    if method is None:
        return
    result = method()
    if inspect.isawaitable(result):
        await result
