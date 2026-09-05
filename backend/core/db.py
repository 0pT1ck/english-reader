"""SQLite connections and schema migrations.

Three separate database files are used (see :mod:`backend.core.config` for why).
Raw ``sqlite3`` is used rather than an ORM for two reasons:

1. Cross-database queries via ``ATTACH`` are a normal operation here (study
   state in ``learning.db`` constantly needs word data from ``dictionary.db``),
   and ORMs make that awkward.
2. Migrations need a project-specific guarantee — an automatic backup of
   ``learning.db`` before any schema change — which is easier to own outright
   than to bolt onto a migration framework.

**Migrations are owned by modules.** Each feature module declares its own
migrations; this module only sequences and applies them. That is what lets a new
feature ship its tables without editing anything that already exists
(architecture rule 6).
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from backend.core.config import get_settings

DatabaseName = Literal["dictionary", "learning", "logs"]

# Connections are per-thread: sqlite3 connections are not safe to share across
# threads, and FastAPI runs synchronous endpoint functions in a thread pool.
_local = threading.local()

_migration_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


def _db_path(name: DatabaseName) -> Path:
    settings = get_settings()
    return {
        "dictionary": settings.dictionary_db,
        "learning": settings.learning_db,
        "logs": settings.logs_db,
    }[name]


def _configure(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs every connection in this project needs."""
    # WAL lets a reader run while a writer commits. Single-user workload, but the
    # background generation task and a browser request can still overlap.
    conn.execute("PRAGMA journal_mode = WAL")
    # SQLite has foreign keys off by default; we rely on them for cascade
    # deletes between articles, sentences and tokens.
    conn.execute("PRAGMA foreign_keys = ON")
    # Rather than failing immediately when another connection holds the write
    # lock, wait. Five seconds is far beyond anything this workload should need.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row


def get_connection(name: DatabaseName) -> sqlite3.Connection:
    """Return this thread's connection to one of the three databases.

    The ``learning`` connection has ``dictionary.db`` attached under the schema
    name ``dict``, so queries can join study state against word data directly::

        SELECT s.*, w.headword
        FROM sense_states s
        JOIN dict.senses w ON w.id = s.sense_id
    """
    cache: dict[str, sqlite3.Connection] = getattr(_local, "connections", None)  # type: ignore[assignment]
    if cache is None:
        cache = {}
        _local.connections = cache

    conn = cache.get(name)
    if conn is not None:
        return conn

    path = _db_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    _configure(conn)

    if name == "learning":
        dictionary_path = _db_path("dictionary")
        dictionary_path.parent.mkdir(parents=True, exist_ok=True)
        # ATTACH creates the file if absent, which is fine: an empty dictionary
        # simply means the import script has not been run yet.
        conn.execute("ATTACH DATABASE ? AS dict", (str(dictionary_path),))

    cache[name] = conn
    return conn


def close_connections() -> None:
    """Close this thread's connections. Used at shutdown and in scripts."""
    cache: dict[str, sqlite3.Connection] | None = getattr(_local, "connections", None)
    if not cache:
        return
    for conn in cache.values():
        conn.close()
    cache.clear()


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #


def backup_learning_db(reason: str) -> Path:
    """Snapshot ``learning.db`` into the backup directory.

    Uses SQLite's own backup API rather than copying the file: with WAL enabled
    a plain file copy can miss committed data still sitting in the ``-wal``
    sidecar, producing a backup that looks fine and isn't.

    ``reason`` becomes part of the filename so the backup directory reads as a
    history ("what happened just before this snapshot").
    """
    settings = get_settings()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:60]
    target = settings.backup_dir / f"learning-{stamp}-{safe_reason}.db"

    source = get_connection("learning")
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()

    return target


def apply_pending_restore() -> Path | None:
    """Swap in a database staged by the admin console's restore.

    Must run before any connection is opened — that is the entire reason restore
    is a two-step operation. At this point no thread holds a handle, so the swap
    is a plain file move with no chance of a half-written database.

    Deleting the ``-wal`` and ``-shm`` sidecars is not optional: they belong to
    the *old* database, and leaving them next to a different file is a
    well-known way to corrupt it.

    Returns the backup path if a restore happened, else ``None``.
    """
    settings = get_settings()
    staged = settings.data_dir / "learning.db.pending"
    if not staged.exists():
        return None

    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = settings.backup_dir / f"learning-{stamp}-before-restore.db"

    if settings.learning_db.exists():
        # Open, snapshot, close — all before the app has any connections. This
        # captures data still sitting in the WAL, which a file copy would miss.
        source = sqlite3.connect(settings.learning_db)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    for suffix in ("", "-wal", "-shm"):
        stale = settings.learning_db.with_name(settings.learning_db.name + suffix)
        stale.unlink(missing_ok=True)

    shutil.move(str(staged), str(settings.learning_db))
    return backup_path


def prune_backups(keep: int = 20) -> list[Path]:
    """Delete all but the newest ``keep`` automatic backups.

    Backups are small, but an unbounded directory on a device with an SSD the
    user also uses for other things is still bad manners.
    """
    settings = get_settings()
    if not settings.backup_dir.exists():
        return []

    backups = sorted(
        settings.backup_dir.glob("learning-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)
        removed.append(stale)
    return removed


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Migration:
    """One versioned schema change belonging to one module.

    ``version`` only needs to be unique and increasing *within* a module, so
    modules can be developed independently without coordinating numbers.

    ``apply`` is either raw SQL (the common case) or a callable for changes that
    need logic — backfilling a column from existing rows, for instance.
    """

    version: int
    name: str
    database: DatabaseName
    apply: str | Callable[[sqlite3.Connection], None]


_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    module      TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    PRIMARY KEY (module, version)
)
"""


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(_MIGRATION_TABLE)
    conn.commit()


def _applied_versions(conn: sqlite3.Connection, module: str) -> set[int]:
    _ensure_migration_table(conn)
    rows = conn.execute(
        "SELECT version FROM schema_migrations WHERE module = ?", (module,)
    ).fetchall()
    return {row["version"] for row in rows}


def run_migrations(module: str, migrations: Iterable[Migration]) -> list[Migration]:
    """Apply a module's pending migrations in version order.

    Returns the migrations that were actually applied, so the caller can log a
    meaningful summary rather than "startup complete".

    Before the first change that touches ``learning.db``, a backup is taken.
    That database holds study records that cannot be regenerated, and every
    phase of this project will add tables to it — so the one thing that must
    never happen is a botched migration with no way back.
    """
    pending = sorted(
        (m for m in migrations), key=lambda m: m.version
    )
    if not pending:
        return []

    with _migration_lock:
        applied: list[Migration] = []
        backed_up = False

        for migration in pending:
            conn = get_connection(migration.database)
            if migration.version in _applied_versions(conn, module):
                continue

            if migration.database == "learning" and not backed_up:
                backup_learning_db(f"before-{module}-v{migration.version}")
                prune_backups()
                backed_up = True

            try:
                if callable(migration.apply):
                    migration.apply(conn)
                else:
                    conn.executescript(migration.apply)

                conn.execute(
                    "INSERT INTO schema_migrations (module, version, name, applied_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        module,
                        migration.version,
                        migration.name,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            applied.append(migration)

        return applied


# --------------------------------------------------------------------------- #
# Small helpers used across modules
# --------------------------------------------------------------------------- #


def table_exists(name: str, database: DatabaseName = "learning") -> bool:
    conn = get_connection(database)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def database_size_bytes(name: DatabaseName) -> int:
    """Size on disk, including the WAL sidecar if present.

    Shown on the admin console's status page; a learning.db that stops growing
    is a useful early sign that events are not being recorded.
    """
    path = _db_path(name)
    if not path.exists():
        return 0
    total = path.stat().st_size
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            total += sidecar.stat().st_size
    return total
