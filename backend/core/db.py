"""SQLite connections and schema migrations.

Five separate database files are used (see :mod:`backend.core.config` for why).
Raw ``sqlite3`` is used rather than an ORM for two reasons:

1. Cross-database queries via ``ATTACH`` are a normal operation here (a mark in
   ``events.db`` constantly needs the sentence it happened in, which is in
   ``content.db``), and ORMs make that awkward.
2. Migrations need a project-specific guarantee — an automatic backup of the
   irreplaceable files before any schema change — which is easier to own
   outright than to bolt onto a migration framework.

**Every connection attaches every other file** (P9 §10). That is what let the
split of ``learning.db`` into three happen without touching the ~170 call sites
that say ``get_connection(...)`` and then name a table: SQLite resolves an
unqualified table name across the attached databases, for writes as well as
reads, and the table names in this project are unique across the files. What a
connection's own file decides is only **where an unqualified `CREATE` lands and
which `schema_migrations` is consulted** — which is exactly what a migration
needs to be precise about, and nothing else has to care.

**Migrations are owned by modules.** Each feature module declares its own
migrations; this module only sequences and applies them. That is what lets a new
feature ship its tables without editing anything that already exists
(architecture rule 6). A migration declares which file it belongs to; if its SQL
has to create a table in *another* file, it qualifies that name with the alias
below — ``review`` v1 does, because the sentences it creates are content while
the session tables beside them are records.
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

DatabaseName = Literal["dictionary", "content", "events", "ops", "logs"]

#: The databases that cannot be regenerated from a download, and therefore the
#: ones every backup, restore and pre-migration snapshot must cover.
#: ``dictionary`` is re-importable, ``logs`` is disposable, and ``ops`` costs a
#: re-pairing and a few settings — all three are deliberately excluded, and that
#: is what keeps a backup small.
BACKED_UP: tuple[DatabaseName, ...] = ("events", "content")

#: The alias every database is attached under, on every connection. The alias is
#: the file's own name except for ``dictionary``, which is ``dict`` because that
#: is what the queries written since P0 say.
#:
#: **A connection never attaches itself** — its own file is ``main``, and
#: attaching the same file twice under two names is a way to get two different
#: answers about one row.
ALIASES: dict[DatabaseName, str] = {
    "dictionary": "dict",
    "content": "content",
    "events": "events",
    "ops": "ops",
    "logs": "logs",
}

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
        "content": settings.content_db,
        "events": settings.events_db,
        "ops": settings.ops_db,
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
    """Return this thread's connection to one of the five databases.

    **Every other file is attached**, so a query can join across them directly
    and an unqualified table name resolves wherever that table actually lives::

        SELECT m.*, s.text, w.headword
        FROM study_marks m                     -- events.db
        JOIN reading_sentences s ON s.id = m.sentence_id   -- content.db
        JOIN words w             ON w.headword = m.item_key     -- dictionary.db

    The aliases are in :data:`ALIASES`; ``dict`` is the ECDICT import.

    **Prefer the unqualified name.** Every table name in this project is unique
    across the five files (``verify_phase9`` asserts it), so an unqualified name
    resolves to the one file that has it — which is what let P9 §10 move twenty
    tables between files without touching the queries. A qualifier is only right
    when the connection's own file is meant, and then only for the tables that
    exist in *every* file: ``schema_migrations`` and the ``sqlite_*`` ones, which
    always resolve to ``main`` anyway.

    **A connection never attaches itself**, so ``content.senses`` is wrong on the
    ``content`` connection and right on the others — another reason to leave the
    prefix off and let SQLite find it.
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

    # ATTACH creates the file if absent, which is fine: an empty database simply
    # means the import or generation step has not been run yet.
    for attached, alias in ALIASES.items():
        if attached == name:
            continue
        attached_path = _db_path(attached)
        attached_path.parent.mkdir(parents=True, exist_ok=True)
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(attached_path),))

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


def backup_database(name: DatabaseName, reason: str) -> Path:
    """Snapshot one database into the backup directory.

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
    target = settings.backup_dir / f"{name}-{stamp}-{safe_reason}.db"

    source = get_connection(name)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()

    return target


def backup_learning_db(reason: str) -> Path:
    """Gone with the file it named (P9 §10).

    Kept as a loud failure rather than silently pointed at ``events``: the two
    are not the same thing, and a caller that still wants "back up the learning
    database" has to say which of the three it means.
    """
    raise RuntimeError(
        "learning.db 在 P9 §10 拆成了 content / events / ops，"
        "这个函数没有对应的文件了——改调 backup_database(\"events\") "
        "或 backup_database(\"content\")")


def apply_pending_restore() -> dict[str, Path]:
    """Swap in databases staged by the admin console's restore.

    Must run before any connection is opened — that is the entire reason restore
    is a two-step operation. At this point no thread holds a handle, so the swap
    is a plain file move with no chance of a half-written database.

    Deleting the ``-wal`` and ``-shm`` sidecars is not optional: they belong to
    the *old* database, and leaving them next to a different file is a
    well-known way to corrupt it.

    Returns ``{database name: path of the pre-restore backup}`` for whatever was
    actually swapped, which is empty on a normal start.
    """
    settings = get_settings()
    restored: dict[str, Path] = {}

    for name in BACKED_UP:
        target = _db_path(name)
        staged = target.with_name(target.name + ".pending")
        if not staged.exists():
            continue

        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = settings.backup_dir / f"{name}-{stamp}-before-restore.db"

        if target.exists():
            # Open, snapshot, close — all before the app has any connections.
            # This captures data still in the WAL, which a file copy would miss.
            source = sqlite3.connect(target)
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

        for suffix in ("", "-wal", "-shm"):
            stale = target.with_name(target.name + suffix)
            stale.unlink(missing_ok=True)

        shutil.move(str(staged), str(target))
        restored[name] = backup_path

    return restored


def prune_backups(keep: int = 20) -> list[Path]:
    """Delete all but the newest ``keep`` automatic backups *per database*.

    Per database rather than overall: a burst of content.db snapshots during a
    generation run must not push every learning.db snapshot out of the window.

    Backups are small, but an unbounded directory on a device with an SSD the
    user also uses for other things is still bad manners.
    """
    settings = get_settings()
    if not settings.backup_dir.exists():
        return []

    removed = []
    for name in BACKED_UP:
        backups = sorted(
            settings.backup_dir.glob(f"{name}-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
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

    Before the first change to a database that cannot be regenerated
    (:data:`BACKED_UP`), a backup of *that* database is taken. Those files hold
    records that cannot be rebuilt from a download, and every phase of this
    project will add tables to them — so the one thing that must never happen is
    a botched migration with no way back.
    """
    pending = sorted(
        (m for m in migrations), key=lambda m: m.version
    )
    if not pending:
        return []

    with _migration_lock:
        applied: list[Migration] = []
        backed_up: set[str] = set()

        for migration in pending:
            conn = get_connection(migration.database)
            if migration.version in _applied_versions(conn, module):
                continue

            if migration.database in BACKED_UP and migration.database not in backed_up:
                backup_database(
                    migration.database, f"before-{module}-v{migration.version}"
                )
                prune_backups()
                backed_up.add(migration.database)

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


def table_exists(name: str, database: DatabaseName = "events") -> bool:
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
