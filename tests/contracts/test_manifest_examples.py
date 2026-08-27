"""Repository-level contract checks for every published Agent Manifest example."""

from pathlib import Path

import pytest
from kitsune_contracts import AgentManifest, load_manifest

EXAMPLE_DIRECTORY = Path(__file__).parents[2] / "config" / "examples" / "agents"
MANIFESTS = sorted(EXAMPLE_DIRECTORY.glob("*.yaml"))


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda path: path.name)
def test_manifest_example_matches_canonical_contract(path: Path) -> None:
    """Every documented Manifest must validate through the shared Pydantic model."""

    manifest = load_manifest(path)
    assert isinstance(manifest, AgentManifest)
    assert manifest.schema == "kitsune.agent"
    assert manifest.revision == 1


def test_manifest_example_ids_are_unique() -> None:
    """Examples must remain loadable as one atomic Workspace definition set."""

    identifiers = [load_manifest(path).metadata.id for path in MANIFESTS]
    assert MANIFESTS
    assert len(identifiers) == len(set(identifiers))
