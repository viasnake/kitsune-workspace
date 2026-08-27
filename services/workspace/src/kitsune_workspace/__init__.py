"""Kitsune Workspace control plane."""

from .app import create_app
from .config import WorkspaceSettings

__all__ = ["WorkspaceSettings", "create_app"]

__version__ = "1.0.0"
