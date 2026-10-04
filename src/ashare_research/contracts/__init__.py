"""Stable project path and data-access contracts.

The contracts package is intentionally small: existing callers may keep using
legacy relative paths while new code resolves through :func:`get_paths`.
"""

from .paths import ProjectPaths, get_paths, project_root

__all__ = ["ProjectPaths", "get_paths", "project_root"]
