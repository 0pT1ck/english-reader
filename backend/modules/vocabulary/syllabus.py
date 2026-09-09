"""Is this word inside a syllabus — the one place that answers it.

考纲判定 syllabus judgement / 派生词 derived word / 词形还原 lemmatisation

The question "is this word beyond the syllabus" is asked from three places: the
generation checker (超纲率 of a draft), article ingest (the ``beyond`` flag on
every token) and the difficulty profile (超四级/超六级词比例). Before this
module they each answered it themselves, and **only the generation checker
answered it correctly** — the other two compared one spelling's tags and
stopped there.

Measured on the 453-article corpus before the fix: of 8,401 tokens flagged as
out of syllabus, **3,005 (35.8%) had a root that is inside CET-6** —
``quickly``←quick 30 times, ``fully``←full 28, ``cultural``←culture 27,
``educational``←education 28, ``entirely``←entire 18. The reading page was
labelling ``quickly`` as out of syllabus and offering to excuse the learner
from learning it.

**This is the fourth time a syllabus lookup in this project measured something
other than what its name said**, after: tags are a levelled list and must be
read cumulatively; tags are split at random between British and American
spellings; and the ``there be`` miscount. Every one of them had the same shape
— a helper existed, one caller used it, the others reimplemented the easy half.
Hence this module: **there is one function, and the three callers call it.**

Four things have to be checked, in this order (cheapest first):

1. the lemma's own tags, unioned across spelling variants (:func:`.repository.tags_of`);
2. the surface form's tags — ``data`` carries the syllabus tag while its lemma
   ``datum`` does not, and ``data`` is what appears on the page;
3. the dictionary's own inflection table — ``planning`` and ``debating`` are
   untagged headwords in their own right, so the lemmatiser never looks
   further, but ECDICT maps them back to ``plan`` and ``debate``;
4. grade-A derivations, **walked to the root of the chain** — a reader who
   knows ``nation`` is not meeting a new word in ``nationality``, which decomposes
   through ``national``. One step is not enough for those.

The grades split three ways, and so does the answer — this is the split the
generation checker has always made, and what the reading side now matches:

* **A** — the suffix is grammar, not vocabulary (``-ly``, ``-ness``, ``-ful``).
  ``carefully`` is not a word to learn if you know ``careful``. :func:`within`.
* **B** — inferable but with meaning added (``nation`` → ``nationality``). Not
  out of syllabus either — the checker's own note says counting ``cooperation``
  as out of syllabus while ``cooperate`` is a CET-4 word "would make the
  zero-out-of-syllabus goal unreachable and meaningless" — but it *is* a
  learning cost, which the design counts as a third of a target word.
  :func:`light_root` answers this one separately so the two stay countable.
* **C** — the meaning has drifted; the breakdown would mislead. Out of syllabus
  like any other word.

Requiring the root to carry a syllabus tag is what keeps the known bad rule
entries harmless. The audit recorded in ``ingest.derivation_for`` found roughly
one rule-only grade-A entry in five is nonsense (``banner``←ban, ``repair``←pair);
those survive here only when the false root is itself a syllabus word, and the
cost when they do is one hard word called easy — not a wrong breakdown shown to
the reader, which is the failure that audit was about.
"""

from __future__ import annotations

from functools import lru_cache

from backend.modules.vocabulary import repository

#: Syllabus tags are a levelled list, not a cumulative one: ``the`` carries
#: ``zk gk`` and no ``cet4`` at all. "Within CET-4" therefore means carrying
#: **any** of these, never "carrying cet4". Reading it the other way put the
#: out-of-syllabus rate at 58% instead of 10%.
WITHIN_CET4 = frozenset({"zk", "gk", "cet4"})
WITHIN_CET6 = WITHIN_CET4 | {"cet6"}

#: How far up a derivation chain to walk. ``nationality → national → nation``
#: is two steps; three is headroom, and the guard that matters is the cycle
#: check rather than the depth.
MAX_DERIVATION_DEPTH = 3


def _families():
    """The word-family module, or ``None``.

    Imported lazily so that vocabulary keeps working when the module is absent
    — the same reason the generation checker has always imported it inside its
    own function.
    """
    try:
        from backend.modules.wordfamily import repository as families
    except ImportError:  # pragma: no cover - module not installed
        return None
    return families


@lru_cache(maxsize=20000)
def transparent_roots(word: str) -> tuple[str, ...]:
    """Every grade-A root above ``word``, nearest first.

    Empty when the word is not a transparent derivation. The chain matters:
    ``nationality`` decomposes through ``national``, so a single step lands on
    a word that is itself untagged and the check fails for no good reason.

    The walk stops on a repeat, so a family table that happens to contain a
    cycle cannot hang the caller.
    """
    families = _families()
    if families is None:
        return ()

    chain: list[str] = []
    seen = {word.lower()}
    current = word.lower()
    for _ in range(MAX_DERIVATION_DEPTH):
        try:
            root = families.transparent_root(current)
        except Exception:  # noqa: BLE001 - a missing family table must not break ingest
            break
        if not root:
            break
        root = root.lower()
        if root in seen:
            break
        chain.append(root)
        seen.add(root)
        current = root
    return tuple(chain)


@lru_cache(maxsize=20000)
def light_root(lemma: str, surface: str, tiers: frozenset[str]) -> str | None:
    """An in-syllabus root this word is a **grade-B** derivation of.

    Deliberately separate from :func:`within`: a B-grade derivation is not out
    of syllabus, but it is not free either. The design counts it as a light
    target word worth a third of a slot, and keeping the two answers apart is
    what lets anything downstream count them apart.
    """
    families = _families()
    if families is None:
        return None
    forms = (lemma.lower(),) if not surface or surface.lower() == lemma.lower() \
        else (lemma.lower(), surface.lower())
    for candidate in forms:
        try:
            family = families.family_of(candidate)
        except Exception:  # noqa: BLE001 - a missing family table must not break ingest
            return None
        if family and family["grade"] == "B":
            root = str(family["root"])
            if repository.tags_of(root) & tiers:
                return root
    return None


@lru_cache(maxsize=50000)
def within(lemma: str, surface: str, tiers: frozenset[str]) -> bool:
    """Whether this word counts as inside ``tiers``.

    ``surface`` is the form as it appears on the page; pass the lemma again
    when there is no separate surface form. See the module docstring for why
    all four checks are needed and what each one catches.
    """
    if not lemma:
        return False

    lemma = lemma.lower()
    surface = (surface or lemma).lower()

    if repository.tags_of(lemma) & tiers:
        return True
    if surface != lemma and repository.tags_of(surface) & tiers:
        return True

    base = repository.resolve_surface(surface)
    if base and repository.tags_of(base) & tiers:
        return True

    for form in (lemma, surface) if surface != lemma else (lemma,):
        for root in transparent_roots(form):
            if repository.tags_of(root) & tiers:
                return True
    return False


def within_cet4(lemma: str, surface: str = "") -> bool:
    return within(lemma, surface or lemma, WITHIN_CET4)


def within_cet6(lemma: str, surface: str = "") -> bool:
    return within(lemma, surface or lemma, WITHIN_CET6)


def known(lemma: str, surface: str = "", tiers: frozenset[str] = WITHIN_CET6) -> bool:
    """Inside the syllabus **or** a derivation of something inside it.

    This is the question the reading side asks: should this word be flagged to
    the learner as out of range. ``nationality`` should not be, and neither
    should ``quickly``; the interface tells them apart by the breakdown already
    stored on the token, not by this flag.
    """
    surface = surface or lemma
    return within(lemma, surface, tiers) or light_root(lemma, surface, tiers) is not None


def clear_caches() -> None:
    """Drop the memo tables. Called when the dictionary or families change."""
    transparent_roots.cache_clear()
    light_root.cache_clear()
    within.cache_clear()
