"""Small control-plane utilities shared by concrete Workspace modules."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from kitsune.logging import is_sensitive_key, normalize_redacted_keys

AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
ACTIVE_RUN_STATUSES = frozenset({"dispatching", "running"})
PENDING_RUN_STATUSES = frozenset({"created", "queued"})


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Attach UTC to timestamps returned without a timezone by SQLite."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def json_size(value: Any) -> int:
    """Return compact UTF-8 JSON size."""

    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def redact(value: Any, keys: set[str] | frozenset[str]) -> Any:
    """Recursively redact values whose normalized key components are sensitive."""

    normalized = normalize_redacted_keys(frozenset(keys))
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]" if is_sensitive_key(key, normalized) else redact(item, normalized)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, normalized) for item in value]
    return value


def redact_text(value: str, secrets: set[str]) -> str:
    """Replace concrete secret values in unstructured process or container output."""

    result = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def redact_values(value: Any, secrets: set[str]) -> Any:
    """Recursively remove resolved secret values from persisted JSON-compatible data."""

    if isinstance(value, dict):
        return {str(key): redact_values(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_values(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def nested(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Read a nested mapping without coupling to generated contract class names."""

    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
