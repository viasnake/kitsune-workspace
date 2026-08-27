"""Require one release tag to match every versioned Kitsune artifact."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

PYTHON_PROJECTS = (
    Path("pyproject.toml"),
    Path("packages/kitsune-contracts/pyproject.toml"),
    Path("packages/kitsune-sdk/pyproject.toml"),
    Path("packages/kitsune-integration-pydantic-ai/pyproject.toml"),
    Path("packages/kitsune-integration-langchain/pyproject.toml"),
    Path("packages/kitsune-plugin-budget/pyproject.toml"),
    Path("packages/kitsune-plugin-langfuse/pyproject.toml"),
    Path("services/workspace/pyproject.toml"),
)


def main() -> None:
    """Fail when the release tag and a package version differ."""

    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    arguments = parser.parse_args()
    root = Path(__file__).parents[2]
    versions: dict[str, str] = {}
    for relative in PYTHON_PROJECTS:
        with root.joinpath(relative).open("rb") as stream:
            project = tomllib.load(stream)["project"]
        versions[str(relative)] = str(project["version"])
    web_path = Path("apps/workspace-web/package.json")
    web_package = json.loads(root.joinpath(web_path).read_text(encoding="utf-8"))
    versions[str(web_path)] = str(web_package["version"])

    mismatches = {path: version for path, version in versions.items() if version != arguments.tag}
    if mismatches:
        details = ", ".join(f"{path}={version}" for path, version in mismatches.items())
        raise SystemExit(f"release tag {arguments.tag} does not match: {details}")
    print(f"release tag {arguments.tag} matches {len(versions)} versioned artifacts")


if __name__ == "__main__":
    main()
