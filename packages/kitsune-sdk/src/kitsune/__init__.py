"""Public Kitsune SDK API for Agent Applications."""

from kitsune_contracts import (
    EventSeverity,
    KitsuneEvent,
    RunOutcome,
    RunSource,
    RunStatus,
    UsageRecord,
)

from .app import (
    SDK_VERSION,
    AppNotRunningError,
    DuplicateRunError,
    HandlerNotFoundError,
    HandlerRegistration,
    KitsuneApp,
)
from .context import ModelCallAdmission, RunCancelled, RunCancelScope, RunContext
from .control import AcceptedRun, create_control_api
from .outbox import (
    EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE,
    EphemeralDeliveryIncompleteError,
    EventOutbox,
    NonRetryableEventDeliveryError,
    OutboxDrainResult,
    OutboxFullError,
    PendingEvent,
    RetryableEventDeliveryError,
)
from .plugins import (
    AppBuilder,
    CriticalPluginHookError,
    KitsunePlugin,
    PluginDependencyError,
    PluginHost,
    PluginMetadata,
    RunningApp,
)
from .settings import KitsuneSettings
from .telemetry import Telemetry, configure_telemetry, current_trace_id
from .workspace import WorkspaceClient

__version__ = SDK_VERSION

__all__ = [
    "EPHEMERAL_DELIVERY_INCOMPLETE_EXIT_CODE",
    "SDK_VERSION",
    "AcceptedRun",
    "AppBuilder",
    "AppNotRunningError",
    "CriticalPluginHookError",
    "DuplicateRunError",
    "EphemeralDeliveryIncompleteError",
    "EventOutbox",
    "EventSeverity",
    "HandlerNotFoundError",
    "HandlerRegistration",
    "KitsuneApp",
    "KitsuneEvent",
    "KitsunePlugin",
    "KitsuneSettings",
    "ModelCallAdmission",
    "NonRetryableEventDeliveryError",
    "OutboxDrainResult",
    "OutboxFullError",
    "PendingEvent",
    "PluginDependencyError",
    "PluginHost",
    "PluginMetadata",
    "RetryableEventDeliveryError",
    "RunCancelScope",
    "RunCancelled",
    "RunContext",
    "RunOutcome",
    "RunSource",
    "RunStatus",
    "RunningApp",
    "Telemetry",
    "UsageRecord",
    "WorkspaceClient",
    "configure_telemetry",
    "create_control_api",
    "current_trace_id",
]
