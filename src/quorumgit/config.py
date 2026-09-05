"""Runtime configuration resolved from QUORUMGIT_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    data_dir: Path
    agent: str | None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "quorumgit.db"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"


def load() -> Config:
    return Config(
        data_dir=Path(
            os.getenv("QUORUMGIT_DATA_DIR", str(Path.home() / ".quorumgit"))
        ).expanduser(),
        agent=os.getenv("QUORUMGIT_AGENT") or None,
    )
