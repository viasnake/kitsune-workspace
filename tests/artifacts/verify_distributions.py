"""Verify the contents of every publishable Python distribution."""

from __future__ import annotations

import argparse
import email
import tarfile
import zipfile
from dataclasses import dataclass
from email.message import Message
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class DistributionContract:
    """Expected metadata and import package for one distribution."""

    name: str
    import_package: str


DISTRIBUTIONS = (
    DistributionContract("kitsune-contracts", "kitsune_contracts"),
    DistributionContract("kitsune-sdk", "kitsune"),
    DistributionContract("kitsune-integration-pydantic-ai", "kitsune_pydantic_ai"),
    DistributionContract("kitsune-integration-langchain", "kitsune_langchain"),
    DistributionContract("kitsune-plugin-budget", "kitsune_plugin_budget"),
    DistributionContract("kitsune-plugin-langfuse", "kitsune_plugin_langfuse"),
    DistributionContract("kitsune-workspace", "kitsune_workspace"),
)
VERSION = "1.0.0"


def _metadata(content: bytes) -> Message:
    return email.message_from_bytes(content)


def _assert_metadata(metadata: Message, contract: DistributionContract) -> None:
    assert metadata["Name"] == contract.name
    assert metadata["Version"] == VERSION
    assert metadata["License-Expression"] == "Apache-2.0"
    assert "LICENSE" in metadata.get_all("License-File", [])


def _assert_clean_members(members: list[str]) -> None:
    prohibited = [
        name
        for name in members
        if "__pycache__" in PurePosixPath(name).parts
        or name.endswith((".pyc", ".pyo"))
        or "referent-table" in name
    ]
    assert prohibited == [], f"prohibited artifact members: {prohibited}"


def _verify_wheel(
    path: Path,
    contracts: dict[str, DistributionContract],
    license_text: bytes,
) -> str:
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        _assert_clean_members(members)
        metadata_members = [name for name in members if name.endswith(".dist-info/METADATA")]
        assert len(metadata_members) == 1, f"{path.name}: expected one METADATA"
        metadata = _metadata(archive.read(metadata_members[0]))
        name = str(metadata["Name"])
        contract = contracts[name]
        _assert_metadata(metadata, contract)
        assert f"{contract.import_package}/py.typed" in members
        license_members = [
            member for member in members if member.endswith(".dist-info/licenses/LICENSE")
        ]
        assert len(license_members) == 1, f"{path.name}: expected one license file"
        assert archive.read(license_members[0]) == license_text
        if contract.name == "kitsune-workspace":
            entry_points = [
                member for member in members if member.endswith(".dist-info/entry_points.txt")
            ]
            assert len(entry_points) == 1, f"{path.name}: expected one entry_points.txt"
            assert b"kitsune = kitsune.cli:main" in archive.read(entry_points[0])
            for required in (
                "kitsune_workspace/alembic.ini",
                "kitsune_workspace/migrations/env.py",
                "kitsune_workspace/migrations/script.py.mako",
                "kitsune_workspace/migrations/versions/0001_workspace_schema.py",
            ):
                assert required in members, f"{path.name}: missing {required}"
        return name


def _verify_sdist(
    path: Path,
    contracts: dict[str, DistributionContract],
    license_text: bytes,
) -> str:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getnames()
        _assert_clean_members(members)
        metadata_members = [name for name in members if name.endswith("/PKG-INFO")]
        assert len(metadata_members) == 1, f"{path.name}: expected one PKG-INFO"
        metadata_file = archive.extractfile(metadata_members[0])
        assert metadata_file is not None
        metadata = _metadata(metadata_file.read())
        name = str(metadata["Name"])
        contract = contracts[name]
        _assert_metadata(metadata, contract)
        roots = {PurePosixPath(member).parts[0] for member in members if member}
        assert len(roots) == 1, f"{path.name}: expected one source root"
        root = roots.pop()
        license_member = archive.extractfile(f"{root}/LICENSE")
        assert license_member is not None, f"{path.name}: missing LICENSE"
        assert license_member.read() == license_text
        assert f"{root}/src/{contract.import_package}/py.typed" in members
        if contract.name == "kitsune-workspace":
            for required in (
                "alembic.ini",
                "migrations/env.py",
                "migrations/script.py.mako",
                "migrations/versions/0001_workspace_schema.py",
            ):
                member = f"{root}/{required}"
                assert member in members, f"{path.name}: missing {member}"
        return name


def main() -> None:
    """Validate one build directory and fail on any distribution drift."""

    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    directory: Path = arguments.directory
    contracts = {contract.name: contract for contract in DISTRIBUTIONS}
    license_text = Path(__file__).parents[2].joinpath("LICENSE").read_bytes()
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    assert len(wheels) == len(contracts), f"expected {len(contracts)} wheels, got {len(wheels)}"
    assert len(sdists) == len(contracts), f"expected {len(contracts)} sdists, got {len(sdists)}"
    wheel_names = {_verify_wheel(path, contracts, license_text) for path in wheels}
    sdist_names = {_verify_sdist(path, contracts, license_text) for path in sdists}
    assert wheel_names == set(contracts)
    assert sdist_names == set(contracts)
    print(f"verified {len(wheels)} wheels and {len(sdists)} source distributions")


if __name__ == "__main__":
    main()
