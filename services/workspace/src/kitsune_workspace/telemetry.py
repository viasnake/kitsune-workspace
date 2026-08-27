"""OpenTelemetry traces, metrics, and JSON log correlation for Workspace."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, cast

from kitsune.logging import normalize_redacted_keys, redact_sensitive_text
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import func, select

from .config import WorkspaceSettings
from .database import Database
from .models import Run, RuntimeInstance
from .util import ACTIVE_RUN_STATUSES, PENDING_RUN_STATUSES, redact

_PROMOTED_LOG_FIELDS = frozenset(
    {
        "agent_id",
        "correlation_id",
        "event",
        "parent_run_id",
        "run_id",
        "runtime_instance_id",
        "trace_id",
    }
)


def _redact_log_text(value: str, keys: frozenset[str], secrets: set[str]) -> str:
    return redact_sensitive_text(value, keys, frozenset(secrets))


def _redact_log_strings(value: Any, keys: frozenset[str], secrets: set[str]) -> Any:
    if isinstance(value, str):
        return _redact_log_text(value, keys, secrets)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _redact_log_strings(item, keys, secrets) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return [_redact_log_strings(item, keys, secrets) for item in sequence]
    return value


class RedactionFilter(logging.Filter):
    """Remove configured fields and concrete secrets before any log exporter sees them."""

    def __init__(self, redacted_keys: set[str], secrets: set[str]) -> None:
        super().__init__()
        self.redacted_keys = normalize_redacted_keys(frozenset(redacted_keys))
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitize one record in place before handlers and exporters receive it."""

        message = record.getMessage()
        try:
            value = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            sanitized = _redact_log_text(message, self.redacted_keys, self.secrets)
        else:
            sanitized_value = _redact_log_strings(
                redact(value, self.redacted_keys), self.redacted_keys, self.secrets
            )
            if isinstance(sanitized_value, Mapping):
                for field in _PROMOTED_LOG_FIELDS:
                    promoted = sanitized_value.get(field)
                    if isinstance(promoted, str) and getattr(record, field, None) is None:
                        setattr(record, field, promoted)
            sanitized = json.dumps(
                sanitized_value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        record.msg = sanitized
        record.args = ()
        for field in _PROMOTED_LOG_FIELDS:
            current = getattr(record, field, None)
            if isinstance(current, str):
                setattr(
                    record,
                    field,
                    _redact_log_text(current, self.redacted_keys, self.secrets),
                )
        details = getattr(record, "details", None)
        if details is not None:
            record.details = _redact_log_strings(
                redact(details, self.redacted_keys), self.redacted_keys, self.secrets
            )
        if record.exc_info:
            exception_type, exception, _ = record.exc_info
            exception_text = _redact_log_text(
                logging.Formatter().formatException(record.exc_info),
                self.redacted_keys,
                self.secrets,
            )
            record.exc_text = exception_text
            if exception_type is not None:
                record.__dict__["exception.type"] = exception_type.__name__
            if exception is not None:
                record.__dict__["exception.message"] = _redact_log_text(
                    str(exception), self.redacted_keys, self.secrets
                )
            record.__dict__["exception.stacktrace"] = exception_text
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _redact_log_text(record.exc_text, self.redacted_keys, self.secrets)
        if record.stack_info:
            record.stack_info = _redact_log_text(
                record.stack_info, self.redacted_keys, self.secrets
            )
        return True


class JSONFormatter(logging.Formatter):
    """Render structured Workspace logs with current trace correlation."""

    def __init__(self, service_name: str, redacted_keys: set[str] | None = None) -> None:
        super().__init__()
        self.service_name = service_name
        self.redacted_keys = normalize_redacted_keys(frozenset(redacted_keys or set()))

    def format(self, record: logging.LogRecord) -> str:
        """Render one sanitized structured record with trace correlation."""

        span = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "service": self.service_name,
            "agent_id": getattr(record, "agent_id", None),
            "runtime_instance_id": getattr(record, "runtime_instance_id", None),
            "run_id": getattr(record, "run_id", None),
            "parent_run_id": getattr(record, "parent_run_id", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "trace_id": f"{span.trace_id:032x}" if span.is_valid else None,
            "event": getattr(record, "event", None),
            "message": _redact_log_text(record.getMessage(), self.redacted_keys, set()),
        }
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = details
        if record.exc_info:
            payload["exception"] = _redact_log_text(
                self.formatException(record.exc_info), self.redacted_keys, set()
            )
        elif record.exc_text:
            payload["exception"] = _redact_log_text(record.exc_text, self.redacted_keys, set())
        if record.stack_info:
            payload["stack"] = _redact_log_text(record.stack_info, self.redacted_keys, set())
        return json.dumps(
            _redact_log_strings(redact(payload, self.redacted_keys), self.redacted_keys, set()),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _signal_endpoint(base: str, signal_name: str) -> str:
    suffix = f"/v1/{signal_name}"
    return base if base.rstrip("/").endswith(suffix) else f"{base.rstrip('/')}{suffix}"


class WorkspaceTelemetry:
    """Concrete telemetry instruments owned by one Workspace application."""

    def __init__(
        self,
        settings: WorkspaceSettings,
        database: Database,
        *,
        span_exporter: SpanExporter | None = None,
        metric_exporter: MetricExporter | None = None,
        log_exporter: LogRecordExporter | None = None,
    ) -> None:
        self.redaction_secrets: set[str] = set()
        for configured in (settings.auth.client_secret, settings.auth.session_secret):
            if configured is not None:
                self.redaction_secrets.add(configured.get_secret_value())
        self.redaction_filter = RedactionFilter(
            settings.security.redacted_keys, self.redaction_secrets
        )
        resource = Resource.create({"service.name": settings.observability.service_name})
        endpoint = settings.observability.otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint and span_exporter is None:
            span_exporter = OTLPSpanExporter(endpoint=_signal_endpoint(endpoint, "traces"))
        if endpoint and metric_exporter is None:
            metric_exporter = OTLPMetricExporter(endpoint=_signal_endpoint(endpoint, "metrics"))
        if endpoint and log_exporter is None:
            log_exporter = OTLPLogExporter(endpoint=_signal_endpoint(endpoint, "logs"))

        self.tracer_provider = TracerProvider(resource=resource)
        if span_exporter is not None:
            self.tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        self.tracer = self.tracer_provider.get_tracer("kitsune.workspace", "1.0.0")

        readers = [PeriodicExportingMetricReader(metric_exporter)] if metric_exporter else []
        self.meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        self.meter = self.meter_provider.get_meter("kitsune.workspace", "1.0.0")
        self.runs_total = self.meter.create_counter("kitsune_runs_total")
        self.run_duration = self.meter.create_histogram("kitsune_run_duration_seconds")
        self.run_failures = self.meter.create_counter("kitsune_run_failures_total")
        self.runtime_restarts = self.meter.create_counter("kitsune_runtime_restarts_total")
        self.event_delivery_failures = self.meter.create_counter(
            "kitsune_event_delivery_failures_total"
        )
        self.model_requests = self.meter.create_counter("kitsune_model_requests_total")
        self.model_input_tokens = self.meter.create_counter("kitsune_model_input_tokens_total")
        self.model_output_tokens = self.meter.create_counter("kitsune_model_output_tokens_total")
        self.model_estimated_cost = self.meter.create_counter("kitsune_model_estimated_cost")
        self.schedule_delay = self.meter.create_histogram("kitsune_schedule_delay_seconds")
        self.meter.create_observable_gauge(
            "kitsune_active_runs", callbacks=[self._active_runs(database)]
        )
        self.meter.create_observable_gauge(
            "kitsune_queued_runs", callbacks=[self._queued_runs(database)]
        )
        self.meter.create_observable_gauge(
            "kitsune_runtime_instances", callbacks=[self._runtime_instances(database)]
        )
        self.meter.create_observable_gauge(
            "kitsune_agent_heartbeat_age_seconds", callbacks=[self._heartbeat_age(database)]
        )
        self.meter.create_observable_gauge(
            "kitsune_event_outbox_size", callbacks=[lambda _: [Observation(0)]]
        )

        self.logger_provider: LoggerProvider | None = None
        if log_exporter is not None:
            self.logger_provider = LoggerProvider(resource=resource)
            self.logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
            logging.getLogger("kitsune.workspace").addHandler(
                LoggingHandler(logger_provider=self.logger_provider)
            )
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            JSONFormatter(settings.observability.service_name, settings.security.redacted_keys)
        )
        workspace_logger = logging.getLogger("kitsune.workspace")
        workspace_logger.addFilter(self.redaction_filter)
        if not any(
            isinstance(handler.formatter, JSONFormatter) for handler in workspace_logger.handlers
        ):
            workspace_logger.addHandler(stream_handler)
        workspace_logger.setLevel(logging.INFO)
        workspace_logger.propagate = False

    def register_secrets(self, *values: str) -> None:
        """Add resolved manifest secrets to the shared pre-export redaction set."""

        self.redaction_secrets.update(value for value in values if value)

    @staticmethod
    def _active_runs(database: Database):
        def callback(_: Any) -> Iterable[Observation]:
            with database.session() as session:
                value = (
                    session.scalar(
                        select(func.count())
                        .select_from(Run)
                        .where(Run.status.in_(ACTIVE_RUN_STATUSES))
                    )
                    or 0
                )
            return [Observation(value)]

        return callback

    @staticmethod
    def _queued_runs(database: Database):
        def callback(_: Any) -> Iterable[Observation]:
            with database.session() as session:
                value = (
                    session.scalar(
                        select(func.count())
                        .select_from(Run)
                        .where(Run.status.in_(PENDING_RUN_STATUSES))
                    )
                    or 0
                )
            return [Observation(value)]

        return callback

    @staticmethod
    def _runtime_instances(database: Database):
        def callback(_: Any) -> Iterable[Observation]:
            with database.session() as session:
                rows = session.execute(
                    select(RuntimeInstance.status, func.count()).group_by(RuntimeInstance.status)
                ).all()
            return [Observation(count, {"status": state}) for state, count in rows]

        return callback

    @staticmethod
    def _heartbeat_age(database: Database):
        def callback(_: Any) -> Iterable[Observation]:
            from .util import ensure_aware, utcnow

            now = utcnow()
            with database.session() as session:
                rows = session.execute(
                    select(RuntimeInstance.agent_id, func.max(RuntimeInstance.last_heartbeat_at))
                    .where(RuntimeInstance.last_heartbeat_at.is_not(None))
                    .group_by(RuntimeInstance.agent_id)
                ).all()
            return [
                Observation((now - ensure_aware(stamp)).total_seconds(), {"agent_id": agent_id})
                for agent_id, stamp in rows
            ]

        return callback

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        """Start one named span with low-cardinality non-null attributes."""

        with self.tracer.start_as_current_span(
            name,
            attributes={key: value for key, value in attributes.items() if value is not None},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                span.add_event("exception", {"exception.type": type(exc).__name__})
                span.set_status(Status(StatusCode.ERROR))
                raise

    def shutdown(self) -> None:
        """Flush exporters during graceful Workspace shutdown."""

        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()
