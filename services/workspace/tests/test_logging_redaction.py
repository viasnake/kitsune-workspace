"""Workspace log promotion and exception redaction regressions."""

from __future__ import annotations

import json
import logging
import sys

from kitsune_workspace.config import WorkspaceSettings
from kitsune_workspace.telemetry import JSONFormatter, RedactionFilter
from kitsune_workspace.util import redact


def test_workspace_filter_promotes_safe_fields_and_redacts_every_text_copy() -> None:
    try:
        error = RuntimeError("workspace-concrete-secret")
        error.add_note("token=workspace-note-secret")
        raise error
    except RuntimeError:
        record = logging.LogRecord(
            "kitsune.workspace",
            logging.ERROR,
            __file__,
            1,
            json.dumps(
                {
                    "event": "kitsune.runtime.failed",
                    "agent_id": "agent-one",
                    "runtime_instance_id": "runtime-one",
                    "correlation_id": "correlation-one",
                    "message": "authorization: Bearer workspace-message-secret",
                    "levelname": "CRITICAL",
                }
            ),
            (),
            sys.exc_info(),
        )
    record.details = {
        "password": "workspace-detail-secret",
        "safe": "retained",
    }
    record.stack_info = "Stack\nworkspace-stack-secret"

    redaction = RedactionFilter(
        {"authorization", "password", "token"},
        {"workspace-concrete-secret", "workspace-stack-secret"},
    )
    assert redaction.filter(record)
    rendered = JSONFormatter("kitsune-workspace", {"authorization", "password", "token"}).format(
        record
    )
    payload = json.loads(rendered)

    for secret in (
        "workspace-concrete-secret",
        "workspace-note-secret",
        "workspace-message-secret",
        "workspace-detail-secret",
        "workspace-stack-secret",
    ):
        assert secret not in rendered
        assert secret not in repr(vars(record))
    assert record.levelname == "ERROR"
    assert payload["event"] == "kitsune.runtime.failed"
    assert payload["agent_id"] == "agent-one"
    assert payload["runtime_instance_id"] == "runtime-one"
    assert payload["correlation_id"] == "correlation-one"
    assert payload["details"] == {"password": "[REDACTED]", "safe": "retained"}
    assert "RuntimeError" in payload["exception"]
    assert record.exc_info is None
    assert record.__dict__["exception.type"] == "RuntimeError"
    assert "workspace-concrete-secret" not in record.__dict__["exception.stacktrace"]


def test_workspace_redaction_matches_snake_camel_and_header_sensitive_components() -> None:
    """Workspace mapping, filter, and formatter paths share the mandatory matcher."""

    keys = WorkspaceSettings.model_validate(
        {"security": {"redacted_keys": []}}
    ).security.redacted_keys
    details = {
        "access_token": "snake-mapping-secret",
        "refreshToken": "camel-mapping-secret",
        "private_key": "private-mapping-secret",
        "X-API-Key": "header-mapping-secret",
        "safe_monkey": "visible",
    }
    record = logging.LogRecord(
        "kitsune.workspace",
        logging.ERROR,
        __file__,
        1,
        (
            "access_token=snake-text-secret refreshToken=camel-text-secret "
            "privateKey=private-text-secret X-API-Key: header-text-secret "
            "x.api.key=qualified-dotted-secret password.value=dotted-password-secret"
        ),
        (),
        None,
    )
    record.details = details

    assert RedactionFilter(set(), set()).filter(record)
    rendered = JSONFormatter("kitsune-workspace", set()).format(record)
    payload = json.loads(rendered)
    sanitized_mapping = redact(details, keys)
    configured_keys = WorkspaceSettings.model_validate(
        {"security": {"redacted_keys": ["tenantSession"]}}
    ).security.redacted_keys
    configured_addition = redact(
        {"X-Tenant-Session": "configured-addition-secret"}, configured_keys
    )

    for secret in (
        "snake-text-secret",
        "camel-text-secret",
        "private-text-secret",
        "header-text-secret",
        "qualified-dotted-secret",
        "dotted-password-secret",
        "snake-mapping-secret",
        "camel-mapping-secret",
        "private-mapping-secret",
        "header-mapping-secret",
    ):
        assert secret not in rendered
        assert secret not in repr(sanitized_mapping)
    assert payload["details"] == {
        "access_token": "[REDACTED]",
        "refreshToken": "[REDACTED]",
        "private_key": "[REDACTED]",
        "X-API-Key": "[REDACTED]",
        "safe_monkey": "visible",
    }
    assert sanitized_mapping == payload["details"]
    assert configured_addition == {"X-Tenant-Session": "[REDACTED]"}


def test_workspace_json_promotion_does_not_override_existing_or_reserved_fields() -> None:
    record = logging.LogRecord(
        "kitsune.workspace",
        logging.WARNING,
        __file__,
        1,
        json.dumps(
            {
                "event": "message-event",
                "agent_id": "message-agent",
                "levelname": "CRITICAL",
                "pathname": "/attacker/path",
            }
        ),
        (),
        None,
    )
    record.event = "existing-event"

    assert RedactionFilter({"token"}, set()).filter(record)

    assert record.event == "existing-event"
    assert record.agent_id == "message-agent"
    assert record.levelname == "WARNING"
    assert record.pathname == __file__
