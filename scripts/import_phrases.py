"""Build the phrase list and the usage table from Collins — P11 步骤 1.1.

Run::

    uv run python scripts/import_phrases.py            # everything
    uv run python scripts/import_phrases.py --dry-run  # count, write nothing

**The list is 考纲表 ∩ 柯林斯** (决定 ①). The syllabus side says *this is worth
knowing*; Collins says *this is a thing, and here is what it means*. Neither
alone works: the syllabus carries 4,099 entries no dictionary has (``box
inquire``, ``manage to do sth.``), and Collins's piecewise emboldening produces
fragments (``on the`` 242 times) that are not phrases at all.

Four tiers decide "Collins has it" (§13, :mod:`backend.modules.phrases.matching`).
A and B are stored outright; **C and D are stored as candidates** for the
one-at-a-time judgement of 决定 ㉑, because roughly a third of them are matched
to the wrong block.

Usage (决定 ⑧) rides along on the same pass, because it comes out of the same
parse: the bold runs of a **WORD** block are how that sense is used, and they
are stored against the sense they belong to — not against the word. Collins
bolds ``contribute to`` under three of ``contribute``'s four senses and means
something different each time; hang it on the word and that is exactly what is
lost.

The syllabus lists are downloaded (CC BY-SA 4.0 — **attribution is owed**, see
`docs/phase-11.html` §4) and cached under ``data/phrase_lists/``; the dictionary
is the local mdx, which is not in the repository.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.db import get_connection, run_migrations  # noqa: E402
from backend.modules.phrases import matching, repository, schema  # noqa: E402
from backend.modules.senses import collins  # noqa: E402

LISTS = ("junior", "senior", "cet4", "cet6")
LIST_URL = "https://raw.githubusercontent.com/2ndLA/english-phrases/main/lists/{}.txt"
DEFAULT_MDX = Path("data/dictionaries/collins-cobuild-2012.mdx")
CACHE = Path("data/phrase_lists")


def reset(conn) -> None:
    """Throw the built list away and rebuild it from the dictionary.

    **Refuses once anything points at it.** Phrase sense ids are part of the
    contract the moment a device has seen them (架构铁律 5), and a mark on
    ``account for`` 的「占比例」 keys on one. While the list is still being
    built nothing points at it and rebuilding is free; after that this is the
    wrong tool and the guard says so instead of quietly orphaning records.

    The allocator is **not** reset: numbers that were handed out stay spent, so
    a rebuilt list can never hand an old number to a new meaning.
    """
    marks = conn.execute(
        "SELECT COUNT(*) FROM study_marks WHERE item_type = 'phrase'").fetchone()[0]
    states = conn.execute(
        "SELECT COUNT(*) FROM study_states WHERE item_type = 'phrase'").fetchone()[0]
    if marks or states:
        raise SystemExit(
            f"拒绝重置：已经有 {marks} 条词组标记、{states} 条词组学习记录指着这张表。"
            "重置会让它们静默失效（架构铁律 5）。")
    for table in ("phrase_senses", "phrase_list", "phrase_candidates", "phrase_excluded"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    conn.commit()


def load_syllabus() -> dict[str, str]:
    """Phrase -> the earliest level that lists it. Cached; downloaded once."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in LISTS:
        path = CACHE / f"{name}.txt"
        if not path.exists():
            print(f"下载 {name}.txt …")
            with urllib.request.urlopen(LIST_URL.format(name)) as response:
                path.write_bytes(response.read())
        for line in path.read_text(encoding="utf-8").splitlines():
            text = matching.normalise(line)
            if matching.is_multiword(text):
                out.setdefault(text, name)
    return out


def load_dictionary(path: Path) -> tuple[dict[str, list[collins.CollinsSense]], set[str]]:
    """Parse every entry once. Returns (blocks by headword, entry keys with content)."""
    from mdict_utils.reader import MDX

    raw: dict[str, str] = {}
    for key, value in MDX(str(path)).items():
        head = key.decode("utf-8", "replace").strip().lower()
        text = value.decode("utf-8", "replace")
        if text.startswith("@@@LINK="):
            continue
        raw.setdefault(head, text)

    blocks: dict[str, list[collins.CollinsSense]] = {}
    entries: set[str] = set()
    unknown = 0
    for head, html in raw.items():
        if collins.is_hollow(html):
            continue
        entries.add(matching.normalise(head))
        try:
            blocks[head] = collins.parse_entry(head, html)
        except collins.UnknownMarker:
            # Criterion 4 of `classify` raises rather than guessing. A marker it
            # has never seen means the dictionary changed, not that this entry
            # can be skipped quietly — so it is counted and reported.
            unknown += 1
    if unknown:
        print(f"  ⚠️ {unknown} 个词条带着没见过的词性标记，跳过了")
    return blocks, entries


def import_phrases(syllabus: dict[str, str], blocks, entries, *, dry_run: bool) -> dict:
    index = matching.build_index(blocks)
    by_head = {matching.normalise(h): h for h in blocks}
    counts = defaultdict(int)
    matched: set[str] = set()
    empty: list[tuple[str, str]] = []

    for phrase, level in sorted(syllabus.items()):
        found = matching.find(phrase, index, entries)
        if not found:
            continue
        matched.add(phrase)
        tier = found[0].tier
        counts[f"tier_{tier.value}"] += 1
        if not tier.is_clean:
            counts["candidates"] += len(found)
            if dry_run:
                continue
            for hit in found:
                block = _block(blocks, by_head, hit.head, hit.block)
                repository.store_candidate(
                    phrase, tier=tier.value, head=hit.head, block=hit.block,
                    bold=hit.bold, gloss_zh=block.gloss_zh if block else "",
                    level=level)
            continue

        senses = _senses_for(found, blocks, by_head)
        if not senses:
            # Collins has the entry but every block in it is a cross-reference
            # (`→see: …`) or carries no Chinese. It is not on the list — 验收 7
            # says every phrase on it has at least one sense — but it is
            # archived with that reason rather than dropped, because "why is
            # `rain cats and dogs` missing" has to have an answer.
            counts["empty"] += 1
            empty.append((phrase, level))
            continue
        counts["senses"] += len(senses)
        if dry_run:
            continue
        phrase_id = repository.upsert_phrase(
            phrase, source_key=found[0].head, tier=tier.value, level=level)
        kept, added = repository.store_senses(phrase_id, senses)
        counts["senses_kept"] += kept
        counts["senses_added"] += added

    # **Both sides of the intersection are archived** (决定 ①), because each
    # side's exclusions are excluded for a different reason and "why is X not on
    # the list" needs an answer either way.
    collins_only = {
        text for text in index.by_bold
        if text not in syllabus and matching.is_multiword(text)
    }
    counts["syllabus_only"] = len(syllabus) - len(matched)
    counts["collins_only"] = len(collins_only)
    if not dry_run:
        repository.store_excluded(
            (text, "syllabus_only", "柯林斯没有这个词条，四档都对不上", level)
            for text, level in syllabus.items() if text not in matched)
        repository.store_excluded(
            (text, "collins_only", "柯林斯收了，考纲表没有", None)
            for text in sorted(collins_only))
        repository.store_excluded(
            (text, "no_gloss", "柯林斯有词条但每一块都没有中文（多半只是交叉引用）", level)
            for text, level in empty)
    return dict(counts)


def _block(blocks, by_head, head: str, index: int):
    for sense in blocks.get(head) or blocks.get(by_head.get(head, ""), []):
        if sense.block_index == index:
            return sense
    return None


def _senses_for(found, blocks, by_head) -> list[dict]:
    """The blocks these matches point at, as rows ready for storage."""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for hit in found:
        if hit.tier is matching.Tier.OWN_ENTRY:
            head = by_head.get(hit.head, hit.head)
            for sense in blocks.get(head, []):
                if sense.kind is collins.Kind.CROSS_REF or not sense.gloss_zh:
                    continue
                key = (head, sense.block_index)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_row(sense, head))
            continue
        sense = _block(blocks, by_head, hit.head, hit.block)
        if sense is None or not sense.gloss_zh:
            continue
        key = (hit.head, sense.block_index)
        if key in seen:
            continue
        seen.add(key)
        out.append(_row(sense, hit.head))
    return out


def _row(sense: collins.CollinsSense, head: str) -> dict:
    return {
        "gloss_zh": sense.gloss_zh,
        "concept_en": sense.concept_en,
        "pos": sense.pos,
        "pos_zh": sense.pos_zh,
        "register": sense.register,
        "pattern": sense.pattern,
        "source_head": head,
        "source_block": sense.block_index,
        "source_ordinal": sense.ordinal,
    }


def import_usage(blocks, *, dry_run: bool) -> dict:
    """Bold runs of WORD blocks, stored against the sense they describe (⑧/⑨)."""
    conn = get_connection("content")
    known = {
        (str(r["headword"]), int(r["source_block"])): int(r["id"])
        for r in conn.execute(
            "SELECT id, headword, source_block FROM senses"
            " WHERE source_dict = ? AND source_block IS NOT NULL",
            (repository.SOURCE_DICT,))
    }
    counts = defaultdict(int)
    for head, senses in blocks.items():
        for sense in senses:
            if sense.kind is not collins.Kind.WORD or not sense.bolds:
                continue
            sense_id = known.get((head, sense.block_index))
            if sense_id is None:
                continue
            texts = []
            for bold in sense.bolds:
                text = bold.strip()
                # A single bolded word is the headword being highlighted, not a
                # collocation. Two words is the shortest thing that says how it
                # is used (`ability to`).
                if len(text.split()) > 1 and text not in texts:
                    texts.append(text)
            if not texts:
                continue
            counts["senses_with_usage"] += 1
            counts["collocations"] += len(texts)
            if not dry_run:
                repository.store_collocations(sense_id, texts)
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description="导入词组清单与用法")
    parser.add_argument("--mdx", type=Path, default=DEFAULT_MDX)
    parser.add_argument("--dry-run", action="store_true", help="只数，不写库")
    parser.add_argument("--skip-usage", action="store_true")
    parser.add_argument("--reset", action="store_true",
                        help="清空已建的清单重来（只在还没有任何标记指着它时可用）")
    args = parser.parse_args()

    run_migrations("phrases", schema.MIGRATIONS)
    if args.reset and not args.dry_run:
        reset(get_connection("content"))
        print("已清空旧清单（发号器不重置）")
    started = time.time()

    syllabus = load_syllabus()
    print(f"考纲表：{len(syllabus)} 条（含空格的）")

    print("解析柯林斯 …")
    blocks, entries = load_dictionary(args.mdx)
    print(f"  词条 {len(blocks)} 个，{time.time() - started:.0f}s")

    counts = import_phrases(syllabus, blocks, entries, dry_run=args.dry_run)
    print("词组：", {k: counts[k] for k in sorted(counts)})

    if not args.skip_usage:
        usage = import_usage(blocks, dry_run=args.dry_run)
        print("用法：", usage)

    if not args.dry_run:
        get_connection("content").commit()
        print("库里现在：", repository.stats())
    print(f"用时 {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
