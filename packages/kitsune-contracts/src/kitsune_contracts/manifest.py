"""Agent Manifest parsing and schema generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import AgentManifest


def parse_manifest_yaml(content: str | bytes) -> AgentManifest:
    """Parse and validate one Agent Manifest from YAML content."""

    loaded: Any = yaml.safe_load(content)
    if not isinstance(loaded, dict):
        raise ValueError("an Agent Manifest must be a YAML object")
    return AgentManifest.model_validate(loaded)


def load_manifest(path: str | Path) -> AgentManifest:
    """Read and validate one Agent Manifest from ``path``."""

    manifest_path = Path(path)
    return parse_manifest_yaml(manifest_path.read_text(encoding="utf-8"))


def manifest_json_schema() -> dict[str, Any]:
    """Return the canonical JSON Schema for Agent Manifest revision 1."""

    return AgentManifest.model_json_schema(mode="validation")
