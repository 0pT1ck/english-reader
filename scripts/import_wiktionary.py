"""Load the Wiktionary sense inventory into ``content.db``.

Run from the project root::

    uv run python scripts/import_wiktionary.py            # 只导入目标词
    uv run python scripts/import_wiktionary.py --all      # 导入全部词条
    uv run python scripts/import_wiktionary.py --download  # 重新下载转储

Source is kaikki.org's parse of English Wiktionary — a 500 MB JSONL file, one
entry per word-and-part-of-speech. The whole dump is taken rather than the
per-word endpoint because seven thousand HTTP requests is both slower and ruder
than one download, and because a local file makes the whole step reproducible
offline.

**Only the sense inventory is imported.** Wiktionary's own definitions are not
used and never shown: a large share of its entries are public-domain 1913
Webster's text, so the prose is a century old and harder than the words it
defines. What we take is the *list* — which concepts this word has — because
that is the thing a model generating from nothing cannot be checked against.
Not copying the wording also keeps CC BY-SA's share-alike clause out of the
project entirely.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from backend.core.config import get_settings  # noqa: E402
from backend.core.db import get_connection, run_migrations  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.modules.senses import schema, screening  # noqa: E402

log = get_logger("scripts.wiktionary")

SOURCE_URL = "https://kaikki.org/dictionary/English/kaikki.org-dictionary-English.jsonl"

# Tags that mark a sense as no longer current usage. Filtering on these is free
# and removes about a third of the raw inventory before a model ever sees it —
# `address` drops from 29 senses to 20.
DEAD_TAGS = frozenset({
    "obsolete", "archaic", "dated", "historical", "rare", "dialectal",
    "poetic", "nonstandard", "misspelling", "obsolete-spelling",
    "alt-of", "form-of", "abbreviation", "initialism", "acronym",
})

# Parts of speech that carry lexical meaning. Wiktionary also lists prefixes,
# suffixes, letters and proper nouns, none of which belong in a sense set.
KEEP_POS = frozenset({"noun", "verb", "adj", "adv"})


def download(destination: Path, *, force: bool = False) -> Path:
    if destination.exists() and not force:
        print(f"已存在，跳过下载：{destination.name}"
              f"（{destination.stat().st_size / 1_048_576:.0f} MB）")
        return destination

    settings = get_settings()
    proxy = settings.proxy_url or os.environ.get("HTTPS_PROXY")
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {SOURCE_URL}")
    started = time.time()

    with httpx.Client(timeout=300.0, proxy=proxy, follow_redirects=True) as client:
        with client.stream("GET", SOURCE_URL) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            with destination.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1_048_576):
                    fh.write(chunk)
                    done = response.num_bytes_downloaded
                    bar = f"{done / 1_048_576:6.0f} MB"
                    if total:
                        bar += f" / {total / 1_048_576:.0f} MB  {done * 100 // total:3d}%"
                    print(f"\r  {bar}", end="", flush=True)
    print(f"\n下载完成，用时 {time.time() - started:.0f} 秒")
    return destination


def _sense_rows(entry: dict) -> list[dict]:
    """Flatten one dump entry into the senses worth keeping."""
    rows = []
    for index, sense in enumerate(entry.get("senses") or [], start=1):
        glosses = sense.get("glosses") or []
        if not glosses:
            continue
        tags = [str(t) for t in (sense.get("tags") or [])]
        rows.append({
            "pos": entry.get("pos"),
            # Joined rather than flattened: Wiktionary nests, and a child sense
            # read without its parent often makes no sense on its own.
            "gloss": " › ".join(str(g) for g in glosses),
            "tags": " ".join(tags),
            "is_dead": 1 if (set(tags) & DEAD_TAGS) else 0,
            "topics": " ".join(str(t) for t in (sense.get("topics") or [])),
            "ordinal": index,
        })
    return rows


def import_dump(source: Path, *, only: set[str] | None = None) -> dict[str, int]:
    conn = get_connection("content")
    conn.execute("DELETE FROM wiktionary_senses")
    conn.commit()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {"lines": 0, "entries": 0, "senses": 0, "dead": 0, "words": 0}
    seen: set[str] = set()
    batch: list[tuple] = []

    print("解析中…")
    started = time.time()
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            stats["lines"] += 1
            if stats["lines"] % 200_000 == 0:
                print(f"\r  已读 {stats['lines']:,} 行，收 {stats['senses']:,} 个义项",
                      end="", flush=True)
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            word = (entry.get("word") or "").strip().lower()
            if not word or (only is not None and word not in only):
                continue
            if (entry.get("pos") or "") not in KEEP_POS:
                continue

            rows = _sense_rows(entry)
            if not rows:
                continue
            stats["entries"] += 1
            seen.add(word)
            for row in rows:
                stats["senses"] += 1
                stats["dead"] += row["is_dead"]
                batch.append((word, row["pos"], row["gloss"], row["tags"],
                              row["is_dead"], row["topics"], row["ordinal"], now))

            if len(batch) >= 5000:
                conn.executemany(
                    "INSERT INTO wiktionary_senses (headword, pos, gloss, tags,"
                    " is_dead, topics, ordinal, imported_at) VALUES (?,?,?,?,?,?,?,?)",
                    batch)
                conn.commit()
                batch.clear()

    if batch:
        conn.executemany(
            "INSERT INTO wiktionary_senses (headword, pos, gloss, tags,"
            " is_dead, topics, ordinal, imported_at) VALUES (?,?,?,?,?,?,?,?)",
            batch)
        conn.commit()

    conn.execute("ANALYZE")
    conn.commit()
    stats["words"] = len(seen)
    stats["seconds"] = int(time.time() - started)
    print(f"\r  读完 {stats['lines']:,} 行")
    return stats


def main() -> int:
    settings = get_settings()
    force = "--download" in sys.argv
    everything = "--all" in sys.argv

    with trace() as tid:
        print(f"trace {tid}\n")
        run_migrations("senses", schema.MIGRATIONS)

        source = download(settings.data_dir / "wiktionary-en.jsonl", force=force)

        only = None
        if not everything:
            only = {word for word, _ in screening.target_words()}
            print(f"只导入目标词表里的 {len(only):,} 个词（加 --all 导入全部）")

        stats = import_dump(source, only=only)

        print("\n完成。")
        print(f"  覆盖到的词   {stats['words']:,}"
              + (f" / {len(only):,}（{100 * stats['words'] // max(1, len(only))}%）" if only else ""))
        print(f"  义项条目     {stats['senses']:,}")
        print(f"  其中已废弃   {stats['dead']:,}"
              f"（{100 * stats['dead'] // max(1, stats['senses'])}%，机械过滤掉）")
        print(f"  用时         {stats['seconds']} 秒")

        log.info(
            "wiktionary.imported",
            f"导入 Wiktionary 义项清单：{stats['words']} 个词，{stats['senses']} 个义项",
            **{k: v for k, v in stats.items() if isinstance(v, int)},
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
