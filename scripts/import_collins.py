"""Import Collins COBUILD into the sense inventory — P10.

Usage::

    uv run python scripts/import_collins.py            # 全量导入
    uv run python scripts/import_collins.py --dry-run  # 只报数，不写库
    uv run python scripts/import_collins.py --word account   # 一个词，看长什么样

**What it does and does not decide.** Parsing and the word/phrase split live in
:mod:`backend.modules.senses.collins`; storage and identity live in
``repository.store_dictionary_senses``. This script is the plumbing between
them, plus the accounting that makes the result checkable: *every* block is
counted somewhere, so "senses in the dictionary" and "rows in the table" can be
reconciled exactly rather than approximately (P10 §11 A1).

**Only the target words.** Collins has 36,328 entries; we import senses for the
7,227 words that already have them — the CET-4/6 vocabulary the corpus is built
on. Pulling in the rest would cost nothing to store and would mean the
annotator's candidate lists suddenly include words no article contains.

**Phrase senses are counted and dropped.** 4,988 of them, and the count is
printed rather than silently discarded: phrases are a phase of their own, and
when that phase starts this number is where it begins.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.db import get_connection, run_migrations  # noqa: E402
from backend.core.registry import run_core_migrations  # noqa: E402
from backend.modules.senses import collins, repository, schema  # noqa: E402

#: Which edition this run is importing. Stored on every row as provenance, and
#: what a re-import matches on — **changing this string makes every sense look
#: new**, which is correct: a different edition is a different source, and the
#: mapping between them is a judgement rather than a join.
SOURCE_DICT = "collins-cobuild-2012"

DEFAULT_MDX = Path("data/dictionaries/collins-cobuild-2012.mdx")


def load_dictionary(path: Path) -> dict[str, str]:
    """The whole mdx as ``headword -> html``.

    Needs ``mdict-utils``. Imported here rather than at module scope so that the
    dependency is only required by the one script that reads a dictionary file.
    """
    from mdict_utils.reader import MDX

    entries: dict[str, str] = {}
    for key, value in MDX(str(path)).items():
        headword = key.decode("utf-8", "replace").strip().lower()
        # First spelling wins: Collins lists inflected forms pointing at the
        # same entry, and the base form comes first.
        entries.setdefault(headword, value.decode("utf-8", "replace"))
    return entries


def target_words() -> list[str]:
    """The words we keep senses for, from whatever inventory is current."""
    rows = get_connection("content").execute(
        "SELECT DISTINCT headword FROM senses_pre_collins"
        " UNION SELECT DISTINCT headword FROM senses"
    ).fetchall()
    return sorted(row[0] for row in rows if row[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mdx", type=Path, default=DEFAULT_MDX)
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析和报数，一行都不写")
    parser.add_argument("--word", help="只处理一个词，用来看解析结果")
    parser.add_argument("--limit", type=int, help="只处理前 N 个词（调试用）")
    args = parser.parse_args()

    if not args.mdx.exists():
        print(f"找不到词典文件：{args.mdx}", file=sys.stderr)
        return 1

    run_core_migrations()          # logs / config / auth / tasks
    run_migrations("senses", schema.MIGRATIONS)

    print(f"读取 {args.mdx} …")
    entries = load_dictionary(args.mdx)
    print(f"  {len(entries):,} 个条目")

    words = [args.word.lower()] if args.word else target_words()
    if args.limit:
        words = words[:args.limit]
    print(f"目标词 {len(words):,} 个")

    tally: collections.Counter[str] = collections.Counter()
    unknown: list[str] = []
    no_word_sense: list[str] = []

    for word in words:
        html = entries.get(word)
        if html is None:
            tally["词典里没有这个词"] += 1
            no_word_sense.append(word)
            continue
        if collins.is_hollow(html):
            tally["空壳词条"] += 1
            no_word_sense.append(word)
            continue

        try:
            blocks = collins.parse_entry(word, html)
        except collins.UnknownMarker as exc:
            # Criterion 4: stop and say so. Guessing here is how senses go
            # missing without anyone noticing.
            unknown.append(str(exc))
            tally["未知标记，跳过"] += 1
            continue

        keep = [b for b in blocks if b.kind is collins.Kind.WORD]
        tally["单词义"] += len(keep)
        tally["词组义（不收）"] += sum(1 for b in blocks
                                       if b.kind is collins.Kind.PHRASE)
        tally["交叉引用（丢）"] += sum(1 for b in blocks
                                       if b.kind is collins.Kind.CROSS_REF)
        if not keep:
            tally["只有词组义，没有单词义"] += 1
            no_word_sense.append(word)
            # **Still call the store.** Nothing to insert, but anything a
            # previous run put there has to retire — otherwise it lingers as an
            # orphan in the candidate lists.
            if not args.dry_run:
                _, _, retired, _ = repository.store_dictionary_senses(
                    word, [], source_dict=SOURCE_DICT)
                tally["退休"] += retired
            continue

        tally["有义项的词"] += 1
        tally["例句（解析到）"] += sum(len(b.examples) for b in keep)
        if args.dry_run:
            if args.word:
                for block in keep:
                    print(f"  #{block.ordinal:>2} [{block.pos:<12}{block.pos_zh}] "
                          f"{block.gloss_zh}")
                    print(f"      {block.concept_en[:110]}")
                    if block.register:
                        print(f"      语域 {block.register}")
                    for en, zh in block.examples[:2]:
                        print(f"      例 {en[:80]}")
            continue

        payload = [{
            "concept_en": b.concept_en,
            "gloss_zh": b.gloss_zh,
            "pos": b.pos,
            "pos_zh": b.pos_zh,
            "register": b.register,
            "pattern": b.pattern,
            "subject": b.subject,
            "source_block": b.block_index,
            "source_ordinal": b.ordinal,
            "examples": b.examples,
        } for b in keep]
        inserted, updated, retired, examples = repository.store_dictionary_senses(
            word, payload, source_dict=SOURCE_DICT)
        tally["新插入"] += inserted
        tally["更新"] += updated
        tally["退休"] += retired
        tally["例句"] += examples

    print()
    for label, count in tally.most_common():
        print(f"  {label:<24} {count:>8,}")

    if no_word_sense:
        print(f"\n  没有单词义的词 {len(no_word_sense)} 个，前 20 个：")
        print("   ", ", ".join(no_word_sense[:20]))

    if unknown:
        print(f"\n  ⚠ 未知标记 {len(unknown)} 处，这些词跳过了：")
        for message in unknown[:10]:
            print("   ", message)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
