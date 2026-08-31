"""Structured JSON logging with context correlation and key redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast

MANDATORY_REDACTED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "apikey",
        "client_secret",
        "private_key",
        "privatekey",
        "password",
        "secret",
        "token",
        "credential",
    }
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_KEY_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
_TEXT_ASSIGNMENT = re.compile(
    r"(?<![\w-])(?P<quote>[\"']?)(?P<key>[A-Za-z][A-Za-z0-9_.-]*)(?P=quote)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?:(?:(?i:bearer|basic))\s+)?"
    r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;]+)'
)


def _key_components(value: str) -> tuple[str, ...]:
    expanded = _CAMEL_CASE_BOUNDARY.sub("_", value)
    return tuple(part.casefold() for part in _KEY_SEPARATOR.split(expanded) if part)


def normalize_redacted_keys(keys: frozenset[str]) -> frozenset[str]:
    """Canonicalize configured additions and retain the mandatory security baseline."""

    normalized = {"_".join(components) for key in keys if (components := _key_components(key))}
    return frozenset(normalized) | MANDATORY_REDACTED_KEYS


@lru_cache(maxsize=128)
def _sensitive_key_patterns(keys: frozenset[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        components
        for key in sorted(normalize_redacted_keys(keys))
        if (components := _key_components(key))
    )


def is_sensitive_key(key: object, keys: frozenset[str]) -> bool:
    """Match a sensitive component across snake, camel, and header-style key names."""

    candidate = _key_components(str(key))
    return any(
        candidate[index : index + len(pattern)] == pattern
        for pattern in _sensitive_key_patterns(keys)
        for index in range(len(candidate) - len(pattern) + 1)
    )


def _redact(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): "[REDACTED]" if is_sensitive_key(key, keys) else _redact(item, keys)
            for key, item in mapping.items()
        }
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return [_redact(item, keys) for item in sequence]
    return value


def redact(value: Any, keys: frozenset[str]) -> Any:
    """Return a recursively redacted copy of JSON-compatible data."""

    return _redact(value, normalize_redacted_keys(keys))


def redact_sensitive_text(
    value: str,
    keys: frozenset[str],
    redacted_values: frozenset[str] = frozenset(),
) -> str:
    """Redact sensitive assignments and known secret values from free-form text."""

    normalized = normalize_redacted_keys(keys)
    parts: list[str] = []
    emitted_through = 0
    search_from = 0
    while match := _TEXT_ASSIGNMENT.search(value, search_from):
        if not is_sensitive_key(match.group("key"), normalized):
            search_from = match.start("value")
            continue
        parts.append(value[emitted_through : match.start()])
        parts.append(
            f"{match.group('quote')}{match.group('key')}{match.group('quote')}"
            f"{match.group('separator')}[REDACTED]"
        )
        emitted_through = match.end()
        search_from = match.end()
    parts.append(value[emitted_through:])
    sanitized = "".join(parts)
    for secret in sorted((item for item in redacted_values if item), key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def _redact_string_values(
    value: Any,
    keys: frozenset[str],
    redacted_values: frozenset[str] = frozenset(),
) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value, keys, redacted_values)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): _redact_string_values(item, keys, redacted_values)
            for key, item in mapping.items()
        }
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return [_redact_string_values(item, keys, redacted_values) for item in sequence]
    return value


def redact_sensitive_data(
    value: Any,
    keys: frozenset[str],
    redacted_values: frozenset[str] = frozenset(),
) -> Any:
    """Redact sensitive mapping keys and key/value text before durable delivery."""

    normalized = normalize_redacted_keys(keys)
    return _redact_string_values(
        redact(value, normalized),
        normalized,
        redacted_values,
    )


class JsonFormatter(logging.Formatter):
    """Format log records as one sanitized JSON object per line."""

    def __init__(
        self,
        *,
        service: str,
        redacted_keys: frozenset[str],
        redacted_values: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._service = service
        self._redacted_keys = normalize_redacted_keys(redacted_keys)
        self._redacted_values = redacted_values

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a standard record and Kitsune correlation attributes."""

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": self._service,
            "agent_id": getattr(record, "agent_id", None),
            "runtime_instance_id": getattr(record, "runtime_instance_id", None),
            "run_id": getattr(record, "run_id", None),
            "parent_run_id": getattr(record, "parent_run_id", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = details
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        sanitized = _redact_string_values(
            redact(payload, self._redacted_keys),
            self._redacted_keys,
            self._redacted_values,
        )
        return json.dumps(sanitized, separators=(",", ":"), default=str)


def configure_logging(
    *,
    service: str,
    redacted_keys: frozenset[str],
    redacted_values: frozenset[str] = frozenset(),
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return the dedicated Kitsune JSON logger."""

    logger = logging.getLogger(service)
    logger.setLevel(level)
    logger.propagate = False
    formatter = JsonFormatter(
        service=service,
        redacted_keys=redacted_keys,
        redacted_values=redacted_values,
    )
    existing = next(
        (handler for handler in logger.handlers if isinstance(handler.formatter, JsonFormatter)),
        None,
    )
    if existing is None:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        existing.setFormatter(formatter)
    return logger


def contextual_logger(
    logger: logging.Logger, **context: Any
) -> logging.LoggerAdapter[logging.Logger]:
    """Bind immutable Run correlation fields to a structured logger."""

    return logging.LoggerAdapter(logger, context)
