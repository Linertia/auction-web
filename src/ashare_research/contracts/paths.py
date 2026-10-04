"""Canonical project paths with backwards-compatible relative aliases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class PathContractError(ValueError):
    """Raised when a configured project root or path is invalid."""


def project_root() -> Path:
    """Return the repository root, optionally overridden for tests/relocation.

    ``SUOLUOSI_APP_ROOT`` is deliberately explicit so a migrated checkout can
    run from another directory without rewriting every historical script.
    """

    override = os.environ.get("SUOLUOSI_APP_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise PathContractError(f"SUOLUOSI_APP_ROOT is not a directory: {root}")
        return root

    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "ashare_research").is_dir():
            return candidate
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").is_file():
        return cwd
    raise PathContractError(f"could not locate project root from {current}")


@dataclass(frozen=True)
class ProjectPaths:
    """Absolute paths for stable project assets.

    Properties do not create directories. Callers decide explicitly whether an
    output directory should be created, which keeps read-only research runs
    safe and preserves the old relative-path layout.
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def training_runs(self) -> Path:
        return self.root / "training-runs"

    @property
    def work(self) -> Path:
        return self.root / ".work"

    @property
    def knowledge(self) -> Path:
        return self.root / "knowledge"

    @property
    def knowledge_raw(self) -> Path:
        return self.knowledge / "raw"

    @property
    def knowledge_index(self) -> Path:
        return self.knowledge / "index"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def contracts(self) -> Path:
        return self.root / "contracts"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def strategies(self) -> Path:
        return self.root / "strategies"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def agent(self) -> Path:
        return self.root / "agent"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def trash(self) -> Path:
        return self.root / "_trash"

    # Database and immutable-input aliases. These preserve the old locations
    # while making the semantic role explicit to new callers.
    @property
    def market_db(self) -> Path:
        return self.data / "market.db"

    @property
    def auction_db(self) -> Path:
        return self.data / "jingjia_stage.db"

    @property
    def auction_raw(self) -> Path:
        return self.data / "jingjia"

    @property
    def backtest_20240910_20260915(self) -> Path:
        return self.data / "backtest_20240910_20260915"

    @property
    def auction_monitor(self) -> Path:
        return self.data / "auction-monitor"

    @property
    def auction_monitor_v2(self) -> Path:
        return self.data / "auction-monitor-v2"

    def resolve(self, value: str | Path) -> Path:
        """Resolve an absolute path or a legacy project-relative path."""

        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.root / candidate).resolve()

    def relative(self, value: str | Path) -> Path:
        """Return a project-relative path and reject paths outside the root."""

        resolved = self.resolve(value)
        try:
            return resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathContractError(f"path is outside project root: {resolved}") from exc

    def require(self, value: str | Path) -> Path:
        """Resolve a path and fail with a useful contract error if absent."""

        resolved = self.resolve(value)
        if not resolved.exists():
            raise PathContractError(f"required project path does not exist: {resolved}")
        return resolved


def get_paths(root: str | Path | None = None) -> ProjectPaths:
    """Construct a path contract, using the environment override if present."""

    return ProjectPaths(Path(root).resolve() if root is not None else project_root())


# Compatibility aliases for small scripts that need constants during the
# transition. New code should prefer ``get_paths().market_db`` etc.
PATHS = get_paths()
ROOT = PATHS.root
DATA_DIR = PATHS.data
OUTPUT_DIR = PATHS.output
MARKET_DB = PATHS.market_db
AUCTION_DB = PATHS.auction_db
AUCTION_RAW = PATHS.auction_raw
