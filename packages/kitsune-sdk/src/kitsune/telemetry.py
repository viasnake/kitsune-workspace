"""OpenTelemetry setup and low-cardinality SDK instruments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer


def configure_telemetry(*, service_name: str, otlp_endpoint: str | None = None) -> Telemetry:
    """Compose with global providers or install process-global OTLP providers once."""

    base_endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    trace_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or base_endpoint
    metric_endpoint = os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or base_endpoint
    resource = Resource.create({"service.name": service_name})
    current_tracer_provider = trace.get_tracer_provider()
    current_meter_provider = metrics.get_meter_provider()
    owned_tracer_provider = (
        TracerProvider(resource=resource)
        if isinstance(current_tracer_provider, trace.ProxyTracerProvider)
        else None
    )
    metric_readers: list[PeriodicExportingMetricReader] = []
    needs_meter_provider = type(current_meter_provider).__name__ == "_ProxyMeterProvider"
    if (trace_endpoint and owned_tracer_provider is not None) or (
        metric_endpoint and needs_meter_provider
    ):
        protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").casefold()
        if protocol in {"grpc"}:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            normalize = _identity_endpoint
        elif protocol in {"http/protobuf", "http"}:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            normalize = _signal_endpoint
        else:
            raise ValueError(f"unsupported OTLP protocol: {protocol}")
        if trace_endpoint and owned_tracer_provider is not None:
            owned_tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=normalize(trace_endpoint, "traces")))
            )
        if metric_endpoint and needs_meter_provider:
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=normalize(metric_endpoint, "metrics"))
                )
            )
    owned_meter_provider = (
        MeterProvider(resource=resource, metric_readers=metric_readers)
        if needs_meter_provider
        else None
    )
    if owned_tracer_provider is not None:
        trace.set_tracer_provider(owned_tracer_provider)
    if owned_meter_provider is not None:
        metrics.set_meter_provider(owned_meter_provider)
    return Telemetry.create(
        service_name,
        owned_tracer_provider=owned_tracer_provider,
        owned_meter_provider=owned_meter_provider,
    )


def _signal_endpoint(endpoint: str, signal: str) -> str:
    normalized = endpoint.rstrip("/")
    return normalized if normalized.endswith(f"/v1/{signal}") else f"{normalized}/v1/{signal}"


def _identity_endpoint(endpoint: str, signal: str) -> str:
    del signal
    return endpoint


def current_trace_id() -> str | None:
    """Return the current valid OpenTelemetry trace ID as lowercase hex."""

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


def set_span_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """Set supported scalar span attributes while omitting absent values."""

    for key, value in attributes.items():
        if value is not None and isinstance(value, str | bool | int | float):
            span.set_attribute(key, value)


@dataclass(frozen=True, slots=True)
class Telemetry:
    """OpenTelemetry handles and instruments used by one Kitsune application."""

    tracer: Tracer
    runs_total: Any
    run_duration_seconds: Any
    active_runs: Any
    run_failures_total: Any
    event_outbox_size: Any
    event_delivery_failures_total: Any
    model_requests_total: Any
    model_input_tokens_total: Any
    model_output_tokens_total: Any
    model_estimated_cost: Any
    owned_tracer_provider: TracerProvider | None = None
    owned_meter_provider: MeterProvider | None = None

    @classmethod
    def create(
        cls,
        instrumentation_scope: str,
        *,
        owned_tracer_provider: TracerProvider | None = None,
        owned_meter_provider: MeterProvider | None = None,
    ) -> Telemetry:
        """Create standard Kitsune instruments without high-cardinality labels."""

        tracer = (
            owned_tracer_provider.get_tracer(instrumentation_scope)
            if owned_tracer_provider is not None
            else trace.get_tracer(instrumentation_scope)
        )
        meter = (
            owned_meter_provider.get_meter(instrumentation_scope)
            if owned_meter_provider is not None
            else metrics.get_meter(instrumentation_scope)
        )
        return cls(
            tracer=tracer,
            runs_total=meter.create_counter("kitsune_runs_total"),
            run_duration_seconds=meter.create_histogram("kitsune_run_duration_seconds", unit="s"),
            active_runs=meter.create_up_down_counter("kitsune_active_runs"),
            run_failures_total=meter.create_counter("kitsune_run_failures_total"),
            event_outbox_size=meter.create_up_down_counter("kitsune_event_outbox_size"),
            event_delivery_failures_total=meter.create_counter(
                "kitsune_event_delivery_failures_total"
            ),
            model_requests_total=meter.create_counter("kitsune_model_requests_total"),
            model_input_tokens_total=meter.create_counter("kitsune_model_input_tokens_total"),
            model_output_tokens_total=meter.create_counter("kitsune_model_output_tokens_total"),
            model_estimated_cost=meter.create_counter("kitsune_model_estimated_cost"),
            owned_tracer_provider=owned_tracer_provider,
            owned_meter_provider=owned_meter_provider,
        )

    def shutdown(self) -> None:
        """Flush providers installed globally by this SDK without invalidating other libraries."""

        if self.owned_tracer_provider is not None:
            self.owned_tracer_provider.force_flush()
        if self.owned_meter_provider is not None:
            self.owned_meter_provider.force_flush()
