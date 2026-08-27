"""Public Pydantic AI integration API."""

from .integration import (
    KitsuneModel,
    MCPServerConfiguration,
    ModelReference,
    ProviderModelConfiguration,
    PydanticAIModelConfiguration,
    create_test_model,
    instrument_pydantic_ai,
    resolve_model,
    run_agent,
    usage_to_record,
)

__all__ = [
    "KitsuneModel",
    "MCPServerConfiguration",
    "ModelReference",
    "ProviderModelConfiguration",
    "PydanticAIModelConfiguration",
    "create_test_model",
    "instrument_pydantic_ai",
    "resolve_model",
    "run_agent",
    "usage_to_record",
]
