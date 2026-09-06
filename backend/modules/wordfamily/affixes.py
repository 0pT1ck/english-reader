"""Import ECDICT's root and affix list, and gloss the affixes in Chinese.

``wordroot.txt`` is JSON despite the extension: 611 entries — 423 roots, 110
prefixes and 78 suffixes — each with an English meaning, a class, and example
words. The affixes are usable as they stand except that the meanings are in
English (``state or quality of``), which is not what belongs in a breakdown
shown mid-reading. Those 188 get translated once, which costs almost nothing.

The roots are imported too but are not used for breakdowns: their example words
cover only about a third of our target vocabulary, so they cannot carry the
derivation work. That is done by rule in :mod:`.derive`, with this list as a
cross-check.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.modules.wordfamily import repository

log = get_logger("wordfamily.affixes")

SOURCE_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/wordroot.txt"

# The source spells the class out ("noun-forming suffix"); we only need the
# three families plus the part of speech a suffix produces.
POS_WORDS = ("noun", "adjective", "adverb", "verb")


def local_path() -> Path:
    return get_settings().data_dir / "wordroot.txt"


def download(*, force: bool = False) -> Path:
    target = local_path()
    if target.exists() and not force:
        return target

    settings = get_settings()
    proxy = settings.proxy_url or os.environ.get("HTTPS_PROXY")
    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60.0, proxy=proxy, follow_redirects=True) as client:
        response = client.get(SOURCE_URL)
        response.raise_for_status()
        target.write_bytes(response.content)

    log.info("affixes.downloaded", f"下载了词根词缀表（{len(response.content) // 1024} KB）")
    return target


# Strips the disambiguating index the source appends to repeated affixes:
# "a-1", "al2", "ary2".
_TRAILING_INDEX = re.compile(r"-?\d+$")


def normalise(key: str) -> list[str]:
    """Turn one source key into the affixes it actually names.

    The source packs variants and disambiguators into the key itself —
    ``able, -ible, -ble``, ``a-1``, ``al2, -ial, -ual`` — so 73 of the 188
    affixes are stored under something no lookup will ever ask for. A breakdown
    needs the gloss for ``-able``; nothing will ever look up
    ``able, -ible, -ble``.

    So each key expands into its variants, with hyphens and the numeric
    disambiguators stripped.
    """
    out: list[str] = []
    for part in key.split(","):
        cleaned = part.strip().strip("-").lower()
        cleaned = _TRAILING_INDEX.sub("", cleaned)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def parse(path: Path | None = None) -> list[dict[str, Any]]:
    data = json.loads((path or local_path()).read_text(encoding="utf-8"))

    rows: list[dict[str, Any]] = []
    for key, entry in data.items():
        raw_class = (entry.get("class") or "").lower()
        if "prefix" in raw_class:
            kind = "prefix"
        elif "suffix" in raw_class:
            kind = "suffix"
        else:
            kind = "root"

        forms_pos = next((word for word in POS_WORDS if word in raw_class), None)
        for affix in normalise(key):
            rows.append(
                {
                    "affix": affix,
                    "kind": kind,
                    "meaning_en": entry.get("meaning"),
                    "forms_pos": forms_pos,
                    "examples": entry.get("example") or [],
                }
            )
    return rows


def import_all(*, force_download: bool = False) -> dict[str, int]:
    path = download(force=force_download)
    rows = parse(path)
    repository.store_affixes(rows)
    stats = repository.affix_stats()
    log.info(
        "affixes.imported",
        f"导入词根词缀 {stats['total']} 条",
        **{k: v for k, v in stats.items() if isinstance(v, int)},
    )
    return stats
