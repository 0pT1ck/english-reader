"""Load the dictionary's multi-word entries.

    uv run python scripts/import_phrases.py

``import_dictionary.py`` skips every row whose headword contains a space, with
the note that phrasal verbs were deferred. That was right at the time and it
discarded 366,502 rows which already hold Chinese glosses for ``get up``,
``account for``, ``look after`` and the rest — the data was on disk the whole
time, behind a one-line filter.

This loads them into their own table rather than into ``words``. Every lookup on
the reading path goes through ``words``; doubling that table so phrases are
reachable would slow the common case to serve the rare one.

**What this table is not.** It is not a list of phrases worth learning. Only six
of the 318k two-to-four-word entries carry a syllabus tag and their frequency
columns are all zero, so a naive match against it finds ``to be`` 69 times and
``the world`` 44 times in seventy-seven articles. It answers exactly one
question — *does this combination have a dictionary entry* — and whether a given
occurrence is really a phrase is decided later, in context, per occurrence.

A separate script from the dictionary import because that one rebuilds
``words`` and ``word_forms`` from scratch; this needs to be runnable on its own.
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import get_settings  # noqa: E402
from backend.core.db import get_connection, run_migrations  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.modules.vocabulary.schema import MIGRATIONS  # noqa: E402

log = get_logger("scripts.import_phrases")

#: Two to four words. One word is not a phrase; beyond four the matcher would
#: be searching for sentences, and nothing in the corpus needs it.
MIN_WORDS, MAX_WORDS = 2, 4


def main() -> int:
    settings = get_settings()
    source = settings.data_dir / "ecdict.csv"
    if not source.exists():
        print(f"找不到 {source}，先运行 scripts/import_dictionary.py 下载词典源文件")
        return 1

    with trace():
        run_migrations("vocabulary", MIGRATIONS)
        conn = get_connection("dictionary")
        conn.execute("DELETE FROM phrases")
        conn.commit()

        csv.field_size_limit(10 ** 7)
        batch: list[tuple] = []
        rows = kept = 0
        started = time.time()

        def flush() -> None:
            if batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO phrases (phrase, word_count, head,"
                    " translation, definition, collins, oxford) VALUES (?,?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()
                conn.commit()

        print("导入词组…")
        with source.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rows += 1
                phrase = (row.get("word") or "").strip().lower()
                if " " not in phrase or not phrase.isascii():
                    continue
                parts = phrase.split()
                # Alphabetic words only: the raw file is full of entries like
                # "3d printing" and "a.m." that a word-sequence matcher can
                # never hit anyway.
                if not (MIN_WORDS <= len(parts) <= MAX_WORDS):
                    continue
                if not all(p.isalpha() for p in parts):
                    continue

                def number(field: str) -> int | None:
                    value = (row.get(field) or "").strip()
                    return int(value) if value.isdigit() else None

                batch.append((
                    phrase, len(parts), parts[0],
                    (row.get("translation") or "").strip() or None,
                    (row.get("definition") or "").strip() or None,
                    number("collins"), number("oxford"),
                ))
                kept += 1
                if len(batch) >= 5000:
                    flush()
        flush()

        by_length = {
            r["word_count"]: r["n"] for r in conn.execute(
                "SELECT word_count, COUNT(*) AS n FROM phrases GROUP BY word_count")
        }
        elapsed = round(time.time() - started, 1)
        print("\n完成。")
        print(f"  读入 {rows:,} 行，收下 {kept:,} 条词组")
        for n in sorted(by_length):
            print(f"    {n} 个词  {by_length[n]:,}")
        print(f"  用时 {elapsed} 秒")
        print("\n  注意：这不是「值得学的词组」清单——带考纲标签的只有几条，词频全是 0。")
        print("  它只回答「这个组合在词典里有没有条目」，是不是词组按每一处的上下文判断。")

        log.info(
            "phrases.imported",
            f"词组导入完成，共 {kept} 条",
            phrases=kept, rows=rows, seconds=elapsed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
