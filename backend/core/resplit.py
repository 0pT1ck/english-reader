"""The one-time split of ``learning.db`` into ``content``, ``events`` and ``ops``.

重切 re-split / 迁移记录 migration record / 幂等 idempotent

**Why this is a separate module and not a migration.** The migration framework
applies a module's changes to *one* database and records them there. This moves
tables and records *between* databases, which is the one thing that framework
cannot express — it is the framework's own bookkeeping that has to move.

**Why it moves tables rather than re-creating them.** Each table is created in
its new home from the exact ``CREATE TABLE`` text SQLite already holds for it,
so every column added by an ``ALTER`` along the way comes across without anyone
having to remember it. Re-running the migrations in the new file would have
produced *a* schema; this produces *the* schema.

**Two foreign keys are deliberately dropped**, because SQLite does not follow a
foreign key across files and a reference that cannot be followed is worse than
none — see :data:`DROP_REFERENCES`.

**It is idempotent and resumable.** Every table is verified row-for-row in its
new home before it is dropped from the old file, so an interrupted run leaves
either the old copy or both, never neither. Running it again finishes the job;
running it on an already-split installation does nothing.

**The old file is emptied, not deleted.** An empty ``learning.db`` beside the
new three reads as "this already happened", and deleting a database nobody asked
to delete is not something this project does on its own.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from backend.core.config import get_settings
from backend.core.db import DatabaseName, _db_path
from backend.core.logging import get_logger

log = get_logger("core.resplit")

#: Where each of ``learning.db``'s tables belongs, and why.
#:
#: The question is the same one the file split has always answered — **what
#: happens if this is lost?** ``content`` costs money to make again, ``events``
#: cannot be made again at all, ``ops`` costs a re-pairing and a few settings.
TABLE_HOMES: dict[str, DatabaseName] = {
    # --- content: a model or the CPU produced it, and it is the same for every
    # learner. Losing it means paying for it again.
    "reading_articles": "content",
    "reading_sentences": "content",
    "reading_tokens": "content",
    "reading_phrases": "content",
    "review_sentences": "content",
    "generation_drafts": "content",
    # --- events: the learning record as the server holds it. The device is the
    # first copy; this is where several devices meet. Nothing can rebuild it.
    "client_events": "events",
    "decisions": "events",
    "learner_pool": "events",
    "learner_pool_reports": "events",
    "reading_progress": "events",
    "study_marks": "events",
    "study_states": "events",
    "review_sessions": "events",
    "review_queue": "events",
    "review_history": "events",
    "spelling_attempts": "events",
    # --- ops: how this installation is set up and what it has been doing.
    "devices": "ops",
    "settings": "ops",
    "task_runs": "ops",
    "llm_providers": "ops",
    "llm_jobs": "ops",
    "llm_job_items": "ops",
    "example_observations": "ops",
}

#: Which ``schema_migrations`` row goes to which file. **Derived by hand and
#: checked by `verify_phase9`**, because getting one wrong is silent: a module
#: whose record is missing from its new file has all of its migrations re-run
#: there, and while ``CREATE TABLE IF NOT EXISTS`` survives that,
#: ``ALTER TABLE ADD COLUMN`` and the two callable migrations do not.
#:
#: These must agree with the ``database=`` each ``Migration`` now declares —
#: that is the invariant, and the verification asserts exactly it.
MIGRATION_HOMES: dict[tuple[str, int], DatabaseName] = {
    ("core.auth", 1): "ops",
    ("core.auth", 2): "ops",
    ("core.config", 1): "ops",
    ("core.logging", 2): "events",
    ("core.tasks", 1): "ops",
    ("example", 1): "ops",
    ("generation", 1): "content",
    ("generation", 4): "content",
    ("llm", 1): "ops",
    ("llm", 2): "ops",
    ("progress", 1): "events",
    ("reading", 1): "content",
    ("reading", 2): "events",
    ("reading", 3): "events",
    ("reading", 4): "content",
    ("reading", 5): "events",
    ("reading", 6): "content",
    ("review", 1): "events",
    ("review", 2): "events",
    ("review", 3): "content",
    ("review", 4): "events",
}

#: Foreign keys that have to go, because the two ends no longer share a file.
#:
#: **SQLite does not follow a foreign key into an attached database.** Left in
#: place, the reference is not merely inert — with ``PRAGMA foreign_keys = ON``
#: an insert looks for the parent table in *this* file and fails. So the clause
#: is removed from the stored schema as the table moves, and from the migration
#: that creates it on a fresh install.
#:
#: **What is actually lost is the cascade**, and in both cases losing it is the
#: better half: deleting an article no longer deletes the record that it was
#: read, and deleting a sentence no longer deletes the answer given to it.
#: An orphaned record is a record; a deleted one is not.
DROP_REFERENCES: dict[str, tuple[str, ...]] = {
    "reading_progress": ("reading_articles",),
    "review_history": ("review_sentences",),
}


#: ``CREATE TABLE|INDEX|TRIGGER [IF NOT EXISTS] <name>`` — the span where the
#: database prefix has to be inserted. Matched rather than string-sliced because
#: the name may be quoted and the ``IF NOT EXISTS`` may or may not be there.
_CREATE = re.compile(
    r"^(\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?)",
    re.I,
)


def _qualify(sql: str, database: str) -> str:
    """Point a stored ``CREATE …`` statement at another database file."""
    match = _CREATE.match(sql)
    if not match:
        raise RuntimeError(f"看不懂这条建表语句，不敢改：{sql[:60]}")
    return sql[: match.end()] + f"{database}." + sql[match.end():]


def _strip_reference(sql: str, table: str) -> str:
    """Remove ``REFERENCES <parent>(...) [ON DELETE ...]`` from one column."""
    for parent in DROP_REFERENCES.get(table, ()):
        sql = re.sub(
            rf"\s*REFERENCES\s+{parent}\s*\([^)]*\)(\s+ON\s+DELETE\s+\w+(\s+\w+)?)?",
            "",
            sql,
            flags=re.I,
        )
    return sql


def _objects(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """The table's own DDL plus every index and trigger defined on it."""
    rows = conn.execute(
        "SELECT type, name, sql FROM legacy.sqlite_master"
        " WHERE tbl_name = ? AND sql IS NOT NULL"
        " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
        (table,),
    ).fetchall()
    return [dict(r) for r in rows]


def _count(conn: sqlite3.Connection, qualified: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {qualified}").fetchone()[0])


def _has_table(conn: sqlite3.Connection, database: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {database}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def pending() -> list[str]:
    """Tables still sitting in ``learning.db``. Empty means the split is done."""
    legacy = get_settings().legacy_learning_db
    if not legacy.exists():
        return []
    conn = sqlite3.connect(legacy)
    try:
        names = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    return sorted(names & set(TABLE_HOMES))


def run() -> dict[str, Any] | None:
    """Move everything across. Returns a summary, or ``None`` if there is
    nothing to do.

    Called from application startup **before the modules install**, so that the
    migration framework finds every record already in the file its declaration
    names and therefore applies nothing.
    """
    settings = get_settings()
    legacy = settings.legacy_learning_db
    todo = pending()
    if not todo:
        return None

    # 动这种手术之前先留一份。备份走 SQLite 自己的 backup API 而不是拷文件——
    # 开着 WAL 直接拷会漏掉还在 -wal 里的已提交数据，而那种备份看上去没问题。
    snapshot = backup_database_file(legacy, "before-p9-resplit")
    log.info("resplit.started",
             f"开始重切 learning.db：{len(todo)} 张表，先备份到 {snapshot.name}",
             tables=len(todo), snapshot=snapshot.name)

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    # 搬运期间关外键:要丢的那两个跨文件外键正在被丢掉，而且从旧文件里 DROP
    # 一张还被别人引用着的表不该在这里失败。
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA busy_timeout = 30000")
    # 旧文件自己用别名挂一次，好让 `legacy.` 这个前缀在 sqlite_master 上也说得通。
    conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
    for name in ("content", "events", "ops"):
        path = _db_path(name)  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True)
        conn.execute(f"ATTACH DATABASE ? AS {name}", (str(path),))

    moved: dict[str, int] = {}
    try:
        for table in todo:
            home = TABLE_HOMES[table]
            source_rows = _count(conn, f"legacy.{table}")

            if _has_table(conn, home, table):
                # 上一轮搬到一半:已经建好而且行数对得上，那就只差把旧的删掉。
                if _count(conn, f"{home}.{table}") == source_rows:
                    conn.execute(f"DROP TABLE legacy.{table}")
                    conn.commit()
                    moved[table] = source_rows
                    continue
                raise RuntimeError(
                    f"{home}.{table} 已经存在但行数对不上"
                    f"（{_count(conn, f'{home}.{table}')} vs {source_rows}）——"
                    "不能靠猜，先人工看一眼")

            for obj in _objects(conn, table):
                # 建到新文件里:在对象名前面加上库名，语句本身一个字不改，
                # 除了那两个跟不过来的外键。
                conn.execute(_qualify(
                    _strip_reference(str(obj["sql"]), table), home))

            conn.execute(
                f"INSERT INTO {home}.{table} SELECT * FROM legacy.{table}")
            landed = _count(conn, f"{home}.{table}")
            if landed != source_rows:
                conn.rollback()
                raise RuntimeError(
                    f"{table}: 搬过去 {landed} 行，原本 {source_rows} 行——没有删原表")
            conn.commit()
            conn.execute(f"DROP TABLE legacy.{table}")
            conn.commit()
            moved[table] = source_rows
            log.info("resplit.table", f"{table} → {home}.db（{source_rows} 行）",
                     table=table, database=home, rows=source_rows)

        records = _move_migration_records(conn)
        # 腾出来的页要真的还给文件系统。不 VACUUM 的话 learning.db 还是 43 MB，
        # **而「空了」这件事是靠肉眼看的**——一个和原来一样大的文件旁边放着三个
        # 新文件，读起来像是拆到一半。
        conn.execute("DETACH DATABASE legacy")
        conn.execute("VACUUM")
    finally:
        conn.close()

    left = pending()
    log.info("resplit.done",
             f"重切完成：{len(moved)} 张表、{records} 条迁移记录；"
             f"learning.db 里还剩 {len(left)} 张",
             tables=len(moved), records=records, remaining=len(left))
    return {"tables": moved, "records": records, "remaining": left,
            "snapshot": str(snapshot)}


def _move_migration_records(conn: sqlite3.Connection) -> int:
    """Move each ``schema_migrations`` row to the file its module now writes to.

    **This is the half that makes the re-split safe.** A record left behind is
    not a missing row — it is a whole module's migrations re-running against a
    file that already has the tables.
    """
    rows = conn.execute(
        "SELECT module, version, name, applied_at FROM legacy.schema_migrations"
    ).fetchall()
    moved = 0
    for row in rows:
        key = (str(row["module"]), int(row["version"]))
        home = MIGRATION_HOMES.get(key)
        if home is None:
            log.warning("resplit.record.unknown",
                        f"迁移记录 {key} 不在计划里，原样留在 learning.db",
                        module=key[0], version=key[1])
            continue
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {home}.schema_migrations ("
            " module TEXT NOT NULL, version INTEGER NOT NULL,"
            " name TEXT NOT NULL, applied_at TEXT NOT NULL,"
            " PRIMARY KEY (module, version))")
        conn.execute(
            f"INSERT OR IGNORE INTO {home}.schema_migrations"
            " (module, version, name, applied_at) VALUES (?,?,?,?)",
            (row["module"], row["version"], row["name"], row["applied_at"]))
        conn.execute(
            "DELETE FROM legacy.schema_migrations WHERE module = ? AND version = ?",
            (row["module"], row["version"]))
        moved += 1
    conn.commit()
    return moved


def backup_database_file(path, reason: str):
    """Snapshot a database that :func:`backup_database` cannot name.

    ``learning.db`` is no longer one of the five, so it has no
    :data:`DatabaseName` — but it is exactly the file that must be snapshotted
    before this runs.
    """
    from datetime import datetime, timezone

    settings = get_settings()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = settings.backup_dir / f"learning-{stamp}-{reason}.db"
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


__all__ = ["TABLE_HOMES", "MIGRATION_HOMES", "DROP_REFERENCES", "pending", "run"]
