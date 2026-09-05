"""Download and import the dictionary into ``dictionary.db``.

Run from the project root::

    uv run python scripts/import_dictionary.py

Safe to run again at any time: it replaces the contents wholesale. Nothing in
``learning.db`` is touched, which is the point of keeping them in separate files
— the dictionary is disposable reference data.

Source is ECDICT's ``ecdict.csv`` (~66 MB, ~340k entries), not the full release
(~7.7M entries), because the long tail is dominated by inflected forms, proper
nouns and noise that would inflate the database without helping a CET learner.

Two things get built here:

* ``words`` — one row per headword, with the syllabus tags and frequency ranks
  everything downstream depends on.
* ``word_forms`` — surface form to headword, expanded from ECDICT's inflection
  field. This is the cross-check for the NLP lemmatiser, not a replacement: it
  cannot tell ``left`` (leave) from ``left`` (direction) on its own.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from backend.core.config import get_settings  # noqa: E402
from backend.core.db import Migration, get_connection, run_migrations  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.modules.vocabulary import repository, schema  # noqa: E402

log = get_logger("scripts.dictionary")

SOURCE_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"

# ECDICT inflection codes. Only the ones that produce a distinct surface form
# are useful for reverse lookup; '0' and '1' describe the relationship in the
# other direction and are handled separately.
FORM_CODES = {
    "p": "past",
    "d": "past_participle",
    "i": "present_participle",
    "3": "third_person",
    "r": "comparative",
    "t": "superlative",
    "s": "plural",
}


def download(destination: Path, *, force: bool = False) -> Path:
    """Fetch the CSV, showing progress. Skips the download if already present."""
    if destination.exists() and not force:
        size_mb = destination.stat().st_size / 1_048_576
        print(f"已存在，跳过下载：{destination.name}（{size_mb:.1f} MB）")
        print("  要重新下载，加上 --force")
        return destination

    settings = get_settings()
    proxy = settings.proxy_url or os.environ.get("HTTPS_PROXY")
    print(f"下载 {SOURCE_URL}")
    if proxy:
        print(f"  经由代理 {proxy}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    with httpx.Client(timeout=120.0, proxy=proxy, follow_redirects=True) as client:
        with client.stream("GET", SOURCE_URL) as response:
            response.raise_for_status()
            # content-length describes the *compressed* stream while iter_bytes
            # yields decompressed data, so comparing the two overshoots 100%.
            # num_bytes_downloaded counts what actually came over the wire.
            total = int(response.headers.get("content-length", 0))
            with destination.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=262_144):
                    fh.write(chunk)
                    on_wire = response.num_bytes_downloaded
                    if total:
                        pct = min(100, on_wire * 100 // total)
                        print(
                            f"\r  {on_wire / 1_048_576:6.1f} / {total / 1_048_576:.1f} MB"
                            f"  {pct:3d}%",
                            end="",
                            flush=True,
                        )
                    else:
                        print(f"\r  {on_wire / 1_048_576:6.1f} MB", end="", flush=True)
    elapsed = time.time() - started
    print(f"\n下载完成，用时 {elapsed:.0f} 秒")
    return destination


def _parse_exchange(exchange: str) -> list[tuple[str, str]]:
    """Turn ``p:left/d:left/i:leaving`` into ``[(surface, code), ...]``."""
    if not exchange:
        return []
    out: list[tuple[str, str]] = []
    for part in exchange.split("/"):
        if ":" not in part:
            continue
        code, _, surface = part.partition(":")
        surface = surface.strip().lower()
        if surface and code in FORM_CODES:
            out.append((surface, code))
    return out


def import_csv(source: Path) -> dict[str, int]:
    """Load the CSV into ``dictionary.db``, replacing whatever was there."""
    # csv fields can be long (ECDICT packs full definitions into one cell).
    csv.field_size_limit(10_000_000)

    conn = get_connection("dictionary")
    print("清空旧数据…")
    conn.execute("DELETE FROM word_forms")
    conn.execute("DELETE FROM words")
    conn.commit()

    words: list[tuple] = []
    forms: list[tuple] = []
    seen: set[str] = set()
    stats = {"rows": 0, "words": 0, "forms": 0, "skipped": 0}

    def flush() -> None:
        if words:
            conn.executemany(
                "INSERT OR IGNORE INTO words (headword, phonetic, definition,"
                " translation, pos_hint, collins, oxford, tags, bnc, frq, exchange)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                words,
            )
            words.clear()
        if forms:
            conn.executemany(
                "INSERT OR IGNORE INTO word_forms (surface, headword, kind)"
                " VALUES (?,?,?)",
                forms,
            )
            forms.clear()
        conn.commit()

    print("导入中…")
    started = time.time()
    with source.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stats["rows"] += 1
            headword = (row.get("word") or "").strip().lower()

            # Multi-word entries are phrases, not headwords. Phrasal verbs are a
            # known gap deferred past P0 — the design says so explicitly — and
            # letting them in now would pollute the single-word vocabulary model.
            if not headword or " " in headword or not headword.isascii():
                stats["skipped"] += 1
                continue
            if headword in seen:
                stats["skipped"] += 1
                continue
            seen.add(headword)

            def as_int(key: str) -> int:
                raw = (row.get(key) or "").strip()
                try:
                    return int(raw)
                except ValueError:
                    return 0

            exchange = (row.get("exchange") or "").strip()
            words.append(
                (
                    headword,
                    (row.get("phonetic") or "").strip() or None,
                    (row.get("definition") or "").strip() or None,
                    (row.get("translation") or "").strip() or None,
                    (row.get("pos") or "").strip() or None,
                    as_int("collins"),
                    as_int("oxford"),
                    (row.get("tag") or "").strip() or None,
                    as_int("bnc"),
                    as_int("frq"),
                    exchange or None,
                )
            )
            stats["words"] += 1

            for surface, code in _parse_exchange(exchange):
                if surface != headword:
                    forms.append((surface, headword, code))
                    stats["forms"] += 1

            if len(words) >= 5000:
                flush()
                print(f"\r  已导入 {stats['words']:,} 个词条", end="", flush=True)

    flush()
    print(f"\r  已导入 {stats['words']:,} 个词条")

    print("建立索引与统计信息…")
    conn.execute("ANALYZE")
    conn.commit()

    stats["seconds"] = int(time.time() - started)
    return stats


def main() -> int:
    force = "--force" in sys.argv
    settings = get_settings()

    with trace() as tid:
        print(f"trace {tid}\n")

        # The dictionary tables belong to the vocabulary module; running its
        # migrations here means the script works on a fresh checkout without
        # having to start the service first.
        run_migrations("vocabulary", schema.MIGRATIONS)

        source = download(settings.data_dir / "ecdict.csv", force=force)
        stats = import_csv(source)
        repository.clear_caches()

        summary = repository.stats()
        print("\n完成。")
        print(f"  词条        {summary['words']:,}")
        print(f"  词形变化    {summary['forms']:,}")
        print(f"  带词频排名  {summary['with_frequency']:,}")
        print("  大纲覆盖：")
        for tag, count in summary["by_tag"].items():
            if count:
                label = repository.SYLLABUS_LABELS.get(tag, tag)
                print(f"    {label:8} {count:,}")
        print(f"\n  跳过 {stats['skipped']:,} 行（词组、非 ASCII、重复）")
        print(f"  用时 {stats['seconds']} 秒")

        log.info(
            "dictionary.imported",
            f"词典导入完成，共 {summary['words']} 个词条",
            words=summary["words"],
            forms=summary["forms"],
            seconds=stats["seconds"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
