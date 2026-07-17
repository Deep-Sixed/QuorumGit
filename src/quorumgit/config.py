"""Runtime configuration resolved from QUORUMGIT_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DATABASE_NAME = "quorumgit"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    data_dir: Path
    pg_instance: str
    pg_port: int | None
    agent: str | None

    @property
    def pg_data_dir(self) -> Path:
        return self.data_dir / "pg0" / "data"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"


def load() -> Config:
    port_raw = os.getenv("QUORUMGIT_PG_PORT", "").strip()
    if port_raw and not port_raw.isdigit():
        raise ConfigError(f"QUORUMGIT_PG_PORT must be an integer, got: {port_raw!r}")
    return Config(
        data_dir=Path(
            os.getenv("QUORUMGIT_DATA_DIR", str(Path.home() / ".quorumgit"))
        ).expanduser(),
        pg_instance=os.getenv("QUORUMGIT_PG_INSTANCE", "quorumgit"),
        pg_port=int(port_raw) if port_raw else None,
        agent=os.getenv("QUORUMGIT_AGENT") or None,
    )
