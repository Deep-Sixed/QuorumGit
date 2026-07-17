"""The single persistent store: an application-owned pg0 PostgreSQL instance.

There is exactly one storage backend and one operational mode. If pg0, the
database, or a required extension is unavailable, every operation fails
loudly with a non-zero exit. There is no fallback store and no external-
database mode.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import urllib.request
from importlib import resources
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

from .config import DATABASE_NAME, Config

REQUIRED_EXTENSIONS = ("pg_jsonschema", "pgcrypto")
REQUIRED_TABLES = (
    "schema_migrations",
    "repositories",
    "agents",
    "tasks",
    "claims",
    "scopes",
    "worktrees",
    "checkpoints",
    "handoffs",
    "approvals",
    "votes",
    "conflict_events",
    "audit_events",
)

# pg_jsonschema is not bundled with pg0; it is installed as a three-file
# drop-in extracted from the pinned upstream release artifact. The files land
# inside pg0's versioned installation directory, so a pg0/PostgreSQL upgrade
# silently removes them — provisioning is idempotent and re-run by `init`,
# and the contract check fails loudly whenever the extension is missing.
PG_JSONSCHEMA_VERSION = "0.3.4"
PG_JSONSCHEMA_DEB_URL = (
    "https://github.com/supabase/pg_jsonschema/releases/download/"
    f"v{PG_JSONSCHEMA_VERSION}/pg_jsonschema-v{PG_JSONSCHEMA_VERSION}"
    "-pg18-amd64-linux-gnu.deb"
)
PG_JSONSCHEMA_DEB_SHA256 = (
    "5c7fe6c810835e4242fb0365b57581c0009b0155200dd4f1266cee434295732f"
)


class StoreError(RuntimeError):
    pass


class ContractViolation(StoreError):
    pass


# ---------------------------------------------------------------- lifecycle


def _pg0_handle(cfg: Config):
    from pg0 import Pg0

    cfg.pg_data_dir.mkdir(parents=True, exist_ok=True)
    return Pg0(
        name=cfg.pg_instance,
        port=cfg.pg_port,
        database=DATABASE_NAME,
        data_dir=str(cfg.pg_data_dir),
    )


def ensure_running(cfg: Config) -> str:
    """Start (or reuse) the pg0 instance and return the database URL."""
    from pg0 import Pg0AlreadyRunningError, Pg0Error

    pg = _pg0_handle(cfg)
    try:
        pg.start()
    except Pg0AlreadyRunningError:
        pass
    except Pg0Error as exc:
        raise StoreError(f"pg0 failed to start: {exc}") from exc
    _ensure_database(pg)
    uri = pg.uri
    if not uri:
        raise StoreError("pg0 reported no connection URI after start.")
    return uri


def instance_status(cfg: Config) -> dict:
    from pg0 import Pg0NotFoundError, Pg0NotRunningError

    pg = _pg0_handle(cfg)
    # This pg0 version reports a stopped/absent instance through the returned
    # InstanceInfo (running=False, uri=None) rather than by raising; the
    # exception arms are kept for older/other pg0 releases.
    try:
        info = pg.info()
    except (Pg0NotRunningError, Pg0NotFoundError):
        return {"running": False}
    if not getattr(info, "running", False) or not info.uri:
        return {"running": False}
    return {"running": True, "uri": info.uri, "port": info.port}


def stop(cfg: Config) -> None:
    from pg0 import Pg0NotRunningError

    try:
        _pg0_handle(cfg).stop()
    except Pg0NotRunningError:
        pass


def destroy(cfg: Config) -> None:
    """Stop the instance and delete its data. Irreversible."""
    import pg0 as pg0mod
    from pg0 import Pg0NotFoundError, Pg0NotRunningError

    try:
        _pg0_handle(cfg).stop()
    except (Pg0NotRunningError, Pg0NotFoundError):
        pass
    try:
        pg0mod.drop(cfg.pg_instance, force=True)
    except Pg0NotFoundError:
        pass


def _ensure_database(pg) -> None:
    """Create the application database if the instance predates it."""
    admin_uri = pg.uri.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE_NAME,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE_NAME))
            )


# ---------------------------------------------------------------- extensions


def _pg0_installation_dir() -> Path:
    root = Path.home() / ".pg0" / "installation"
    versions = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not versions:
        raise StoreError(f"No pg0 PostgreSQL installation found under {root}")
    return versions[-1]


def _install_pg_jsonschema_dropin() -> None:
    install = _pg0_installation_dir()
    lib_dir = install / "lib"
    ext_dir = install / "share" / "extension"
    if not lib_dir.is_dir() or not ext_dir.is_dir():
        raise StoreError(f"Unexpected pg0 installation layout at {install}")

    with tempfile.TemporaryDirectory(prefix="quorumgit-ext-") as tmp:
        tmp_path = Path(tmp)
        deb = tmp_path / "pg_jsonschema.deb"
        try:
            urllib.request.urlretrieve(PG_JSONSCHEMA_DEB_URL, deb)
        except OSError as exc:
            raise StoreError(
                "pg_jsonschema is not installed and the pinned artifact could "
                f"not be downloaded from {PG_JSONSCHEMA_DEB_URL}: {exc}"
            ) from exc
        digest = hashlib.sha256(deb.read_bytes()).hexdigest()
        if digest != PG_JSONSCHEMA_DEB_SHA256:
            raise StoreError(
                "pg_jsonschema artifact checksum mismatch: "
                f"expected {PG_JSONSCHEMA_DEB_SHA256}, got {digest}. "
                "Refusing to install."
            )
        extracted = tmp_path / "extracted"
        result = subprocess.run(
            ["dpkg-deb", "-x", str(deb), str(extracted)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StoreError(f"dpkg-deb extraction failed: {result.stderr.strip()}")

        so_files = list(extracted.rglob("pg_jsonschema.so"))
        control_files = list(extracted.rglob("pg_jsonschema*.control")) + list(
            extracted.rglob("pg_jsonschema--*.sql")
        )
        if not so_files or not control_files:
            raise StoreError("Extension artifact did not contain the expected files.")
        # Atomic per-file install: write beside the target, then rename.
        for src, dest_dir in [(f, lib_dir) for f in so_files] + [
            (f, ext_dir) for f in control_files
        ]:
            staged = dest_dir / (src.name + ".staged")
            staged.write_bytes(src.read_bytes())
            staged.replace(dest_dir / src.name)


def provision_extensions(uri: str) -> None:
    with psycopg.connect(uri, autocommit=True) as conn:
        available = {
            row[0]
            for row in conn.execute("SELECT name FROM pg_available_extensions")
        }
        if "pg_jsonschema" not in available:
            _install_pg_jsonschema_dropin()
        for ext in REQUIRED_EXTENSIONS:
            try:
                conn.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                        sql.Identifier(ext)
                    )
                )
            except psycopg.Error as exc:
                raise StoreError(
                    f"Required extension {ext!r} could not be installed: {exc}"
                ) from exc


# ---------------------------------------------------------------- migrations


def migrate(uri: str) -> list[str]:
    """Apply pending SQL migrations in filename order. Returns applied names."""
    migration_files = sorted(
        (
            f
            for f in resources.files("quorumgit.migrations").iterdir()
            if f.name.endswith(".sql")
        ),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with psycopg.connect(uri) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        for mig in migration_files:
            if mig.name in done:
                continue
            conn.execute(cast(LiteralString, mig.read_text(encoding="utf-8")))
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (mig.name,)
            )
            applied.append(mig.name)
        conn.commit()
    return applied


# ------------------------------------------------------------ contract check


def verify_contract(uri: str) -> None:
    """Fail loudly unless the store satisfies the full runtime contract."""
    try:
        with psycopg.connect(uri) as conn:
            installed = {
                row[0] for row in conn.execute("SELECT extname FROM pg_extension")
            }
            missing_ext = set(REQUIRED_EXTENSIONS) - installed
            if missing_ext:
                raise ContractViolation(
                    f"Missing required extensions: {sorted(missing_ext)}. "
                    "Run `quorumgit init` to provision them."
                )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            missing_tables = set(REQUIRED_TABLES) - tables
            if missing_tables:
                raise ContractViolation(
                    f"Missing required tables: {sorted(missing_tables)}. "
                    "Run `quorumgit init` to apply migrations."
                )
            row = conn.execute(
                "SELECT jsonb_matches_schema('{\"type\":\"object\"}'::json, '{}'::jsonb)"
            ).fetchone()
            if row is None or row[0] is not True:
                raise ContractViolation("jsonb_matches_schema is not functional.")
    except psycopg.Error as exc:
        raise StoreError(f"Store is unreachable: {exc}") from exc


def connect(cfg: Config) -> psycopg.Connection:
    """Connection for normal operations: store must be up and contract-valid."""
    uri = ensure_running(cfg)
    try:
        return psycopg.connect(uri)
    except psycopg.Error as exc:
        raise StoreError(f"Cannot connect to the store: {exc}") from exc
