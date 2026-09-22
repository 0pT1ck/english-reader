"""Finding, in Collins, the block that is about a given phrase — P11 §13.

对号 matching / 档 tier / 占位词 placeholder / 碎片 fragment

**The list is an intersection** (决定 ①): a phrase is on it when both the
syllabus phrase list and Collins have it. Each side blocks the other's failure
mode — Collins throws out ``manage to do sth``, the syllabus throws out the
fragments (``on the``, ``is not``, ``at a``) that Collins's piecewise
emboldening produces.

**"Collins has it" is not one question.** The dictionary files phrases in four
different places, and each needs its own lookup:

===== ============================================== ======== =========
tier   where                                          measured  clean?
===== ============================================== ======== =========
A      the phrase is its own entry (``account for``)   976      yes
B      one bold run of a PHRASE block equals it        956      yes, 14/14 sampled
C      equal once placeholders are dropped             ~70      ~3 in 4
D      the block covers several variants at once       ~360     ~2 in 3
===== ============================================== ======== =========

A and B are taken as they stand. **C and D are asked about one at a time**
(决定 ㉑) — they are wrong often enough that taking them wholesale would put
``do harm to`` on the list glossed 「也许值得（做某事）」.

**Bold runs are compared one at a time, never joined.** Joining them first is
how the first attempt matched ``on one's account`` (因为某人的缘故) to
``on account`` (赊账) and ``on the top of`` to ``on top of the world`` — 6 wrong
in a sample of 18. This is 踩过的坑 §10 二 written as code: Collins emboldens a
phrase with a variable in the middle in pieces, so the pieces mean nothing until
something says how they go back together. Tier D is the *only* place joining
happens, and everything it produces goes to the judge.

**Tier B splits in two, and the controls are what found it.** The first run
seeded 30 positive controls from tier B — pairs believed to be right — and the
judge rejected 6. It was right about all six, and the reason is visible in the
bold runs:

* ``as far as`` + ``is concerned`` is *one* phrase emboldened in pieces, so
  ``as far as`` alone is half of it (likewise ``for all`` + ``are worth``);
* ``beside yourself with`` + ``beside the point`` are *two different* phrases in
  one block, and the block's Chinese — 「非常；极度」 — describes only the first.

Either way the gloss on offer is not the gloss for the phrase, which is the one
failure this project cannot have. So a bold run only counts as a clean match
when it is **the whole of what that block emboldens**; a block that bolds
anything else as well produces a ``B*`` candidate for the judge. Sampling 14
tier-B matches had shown 14 correct — the controls found what sampling did not,
which is the whole reason for having them (踩过的坑 §4.3).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from backend.modules.senses.collins import CollinsSense, Kind

#: Words that stand in for whatever the learner puts there. Both sides are
#: stripped before a tier-C comparison: the syllabus writes ``take sth into
#: account`` and Collins writes ``take something into account``.
#:
#: ``sb's`` and ``one's`` are in here as whole tokens, which is why
#: :func:`normalise` keeps the apostrophe. Splitting them into ``sb`` + ``'s``
#: is what made ``break sb's heart`` and ``behind sb's back`` unmatchable on the
#: first pass — both are real phrases Collins carries.
PLACEHOLDERS = frozenset("""
    sth sth's sb sb's sbs somebody somebody's someone someone's something
    one one's ones oneself yourself himself herself themselves myself ourselves
    your his her their its our my you do doing done
""".split())

#: Prepositions Collins sometimes leaves off the bold run because the entry's
#: own grammar note carries them: it prints ``in charge``, the syllabus writes
#: ``in charge of``. Trimming one of these off the *end* produces a tier-D
#: candidate, never an accepted match.
TRAILING_PREPOSITIONS = frozenset("of to for with in on at from about by into".split())

_KEEP = re.compile(r"[^a-z' ]+")
_WS = re.compile(r"\s+")


class Tier(str, Enum):
    OWN_ENTRY = "A"
    BOLD_EQUAL = "B"
    BOLD_SHARED = "B*"
    PLACEHOLDER_EQUAL = "C"
    JOINED_COVER = "D"

    @property
    def is_clean(self) -> bool:
        """Whether this tier is taken as it stands rather than judged (㉑)."""
        return self in (Tier.OWN_ENTRY, Tier.BOLD_EQUAL)


@dataclass(frozen=True, slots=True)
class Match:
    """One block that is (or may be) about one phrase."""

    phrase: str
    tier: Tier
    head: str
    """The entry it was found under. For tier A this is the phrase itself."""
    block: int
    """Position in document order — the stable half of the provenance triple."""
    bold: str
    """What Collins actually emboldened, kept for the judge to look at."""


def normalise(text: str) -> str:
    """Compare-ready form: lowercase, letters and apostrophes, single spaces.

    **The apostrophe is deliberately kept.** Dropping it turns ``sb's`` into two
    tokens and the placeholder rule stops recognising it; see
    :data:`PLACEHOLDERS`.
    """
    text = (text or "").lower().replace("’", "'").strip()
    return _WS.sub(" ", _KEEP.sub(" ", text)).strip()


def strip_placeholders(text: str) -> str:
    """The phrase without its stand-ins: ``take sth into account`` → ``take into account``."""
    return " ".join(w for w in text.split() if w not in PLACEHOLDERS)


def trim_trailing_preposition(text: str) -> str:
    """``in charge of`` → ``in charge``. Returns the text unchanged if it does not end in one."""
    words = text.split()
    if len(words) > 2 and words[-1] in TRAILING_PREPOSITIONS:
        return " ".join(words[:-1])
    return text


def is_multiword(text: str) -> bool:
    return " " in text


@dataclass
class Index:
    """Everything the tiers need, built once over the whole dictionary."""

    #: normalised bold run -> the phrase blocks that emboldened exactly that
    by_bold: dict[str, list[tuple[str, int]]]
    #: the same, with placeholders dropped
    by_bold_stripped: dict[str, list[tuple[str, int]]]
    #: (head, block) -> how many distinct multiword runs that block emboldens.
    #: More than one means the block is about more than this phrase, or about a
    #: phrase this one is only a piece of — either way the judge decides.
    bold_count: dict[tuple[str, int], int]
    #: (head, block) -> that block's bold runs joined, placeholders dropped,
    #: padded with spaces so a lookup is word-aligned
    joined: dict[tuple[str, int], str]


def build_index(blocks: dict[str, list[CollinsSense]]) -> Index:
    """Index every PHRASE block in the dictionary, by entry headword."""
    by_bold: dict[str, list[tuple[str, int]]] = defaultdict(list)
    by_stripped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    joined: dict[tuple[str, int], str] = {}
    bold_count: dict[tuple[str, int], int] = {}

    for head, senses in blocks.items():
        for sense in senses:
            if sense.kind is not Kind.PHRASE or not sense.bolds:
                continue
            where = (head, sense.block_index)
            for bold in sense.bolds:
                text = normalise(bold)
                if not is_multiword(text):
                    continue
                by_bold[text].append(where)
                stripped = strip_placeholders(text)
                if is_multiword(stripped):
                    by_stripped[stripped].append(where)
            distinct = {normalise(b) for b in sense.bolds}
            bold_count[where] = sum(1 for b in distinct if is_multiword(b))
            whole = strip_placeholders(normalise(" ".join(sense.bolds)))
            if is_multiword(whole):
                joined[where] = f" {whole} "
    return Index(by_bold=dict(by_bold), by_bold_stripped=dict(by_stripped),
                 bold_count=bold_count, joined=joined)


def find(phrase: str, index: Index, entries: set[str]) -> list[Match]:
    """Every block that is, or might be, about this phrase — best tier first.

    Stops at the first tier that answers: a phrase with its own entry is not
    also looked for inside other entries, because its own entry is what the
    lexicographers wrote for it.
    """
    text = normalise(phrase)
    if not is_multiword(text):
        return []

    if text in entries:
        return [Match(text, Tier.OWN_ENTRY, text, -1, text)]

    if text in index.by_bold:
        return [Match(text,
                      Tier.BOLD_EQUAL if index.bold_count.get((head, block), 1) <= 1
                      else Tier.BOLD_SHARED,
                      head, block, text)
                for head, block in index.by_bold[text]]

    stripped = strip_placeholders(text)
    if is_multiword(stripped) and stripped in index.by_bold_stripped:
        return [Match(text, Tier.PLACEHOLDER_EQUAL, head, block, stripped)
                for head, block in index.by_bold_stripped[stripped]]

    out: list[Match] = []
    for probe in (stripped, trim_trailing_preposition(stripped)):
        if not is_multiword(probe):
            continue
        needle = f" {probe} "
        for (head, block), whole in index.joined.items():
            if needle in whole:
                out.append(Match(text, Tier.JOINED_COVER, head, block, whole.strip()))
        if out:
            break
    return out
