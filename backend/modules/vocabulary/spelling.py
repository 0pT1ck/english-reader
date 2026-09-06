"""British and American spellings of the same word.

The problem this solves is not "the word is missing" — ECDICT has both
spellings. It is that the **syllabus tags are split arbitrarily between them**::

    neighbor   ky toefl              neighbour  zk gk cet4 cet6
    theater    ky toefl              theatre    zk gk cet4 cet6 ielts
    color      cet4 ky ielts         colour     zk gk
    catalog    cet4 cet6 ky ielts    catalogue  gk cet6 ielts

So a CET-4 learner reading ``neighbor`` is told it is out of syllabus, which is
nonsense — and it inflated the out-of-syllabus rate of every draft in the first
round. The fix is to treat the two spellings as one word and union their tags.

Two guards keep the rules from inventing pairs:

1. The candidate must exist in the dictionary.
2. Their Chinese glosses must share a term.

The second is what makes this safe. Rules alone happily turn ``size`` into
``sise`` and ``member`` into ``membre``, both of which *are* in the dictionary —
as a personal name and a Belgian place name. Real pairs share a gloss
(``colour``/``color`` are identical, ``metre``/``meter`` both say 公尺);
coincidences never do.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Suffix swaps, applied in both directions. Ordered longest-first so that
# -ization is tried before -ize.
SUFFIX_PAIRS: tuple[tuple[str, str], ...] = (
    ("ization", "isation"),
    ("izations", "isations"),
    ("izer", "iser"),
    ("izers", "isers"),
    ("ize", "ise"),
    ("izes", "ises"),
    ("yze", "yse"),
    ("yzes", "yses"),
    ("our", "or"),
    ("ours", "ors"),
    ("re", "er"),
    ("res", "ers"),
    ("ence", "ense"),
    ("ences", "enses"),
    ("ogue", "og"),
    ("ogues", "ogs"),
    ("mme", "m"),
    ("mmes", "ms"),
    ("aemia", "emia"),
    ("oeuvre", "euver"),
)

# Swaps that happen inside the word rather than at the end.
INFIX_PAIRS: tuple[tuple[str, str], ...] = (
    ("ll", "l"),      # woollen / woolen, travelling / traveling
    ("ae", "e"),      # anaemia / anemia
    ("oe", "e"),      # oestrogen / estrogen
)

# Pairs no rule produces. Short list on purpose: anything a rule can generate
# does not belong here.
IRREGULAR: tuple[tuple[str, str], ...] = (
    ("tyre", "tire"), ("pyjamas", "pajamas"), ("plough", "plow"),
    ("draught", "draft"), ("grey", "gray"), ("mould", "mold"),
    ("smoulder", "smolder"), ("aluminium", "aluminum"), ("storey", "story"),
    ("kerb", "curb"), ("cosy", "cozy"), ("sceptic", "skeptic"),
    ("moustache", "mustache"), ("aeroplane", "airplane"), ("axe", "ax"),
    ("judgement", "judgment"), ("ageing", "aging"), ("enquire", "inquire"),
    ("gaol", "jail"), ("cheque", "check"), ("practise", "practice"),
    ("licence", "license"), ("kilogramme", "kilogram"), ("sulphur", "sulfur"),
)

_IRREGULAR_MAP: dict[str, set[str]] = {}
for _a, _b in IRREGULAR:
    _IRREGULAR_MAP.setdefault(_a, set()).add(_b)
    _IRREGULAR_MAP.setdefault(_b, set()).add(_a)

# Chinese gloss lines in ECDICT look like "n. 邻居, 邻接的东西". Strip the part
# of speech and the bracketed subject labels before comparing.
_POS_PREFIX = re.compile(r"^\s*(?:[a-z]{1,5}\.\s*)+|^\s*\[[^\]]{1,8}\]\s*")
_SPLIT = re.compile(r"[,，;；/、]")


def candidates(word: str) -> set[str]:
    """Every spelling this word might also be written as. No validation."""
    word = word.lower()
    out: set[str] = set(_IRREGULAR_MAP.get(word, ()))

    for left, right in SUFFIX_PAIRS:
        for source, target in ((left, right), (right, left)):
            if word.endswith(source) and len(word) - len(source) >= 2:
                out.add(word[: -len(source)] + target)

    for left, right in INFIX_PAIRS:
        for source, target in ((left, right), (right, left)):
            if source in word[1:-1]:
                # Every position, since the relevant letters can repeat.
                for match in re.finditer(re.escape(source), word):
                    start, end = match.span()
                    if start == 0:
                        continue
                    out.add(word[:start] + target + word[end:])

    out.discard(word)
    return out


def gloss_terms(translation: str | None) -> set[str]:
    """Chinese terms in an ECDICT translation, part-of-speech markers removed."""
    if not translation:
        return set()
    terms: set[str] = set()
    for line in translation.replace("\\n", "\n").split("\n"):
        line = _POS_PREFIX.sub("", line)
        for piece in _SPLIT.split(line):
            piece = piece.strip().strip(".。 ")
            # Two characters is the threshold that separates real terms from
            # coincidental single characters; every genuine pair tested shares
            # at least one two-character term.
            if len(piece) >= 2:
                terms.add(piece)
    return terms


def same_word(translation_a: str | None, translation_b: str | None) -> bool:
    """Whether two entries look like the same word spelled differently."""
    a, b = gloss_terms(translation_a), gloss_terms(translation_b)
    return bool(a and b and (a & b))


@lru_cache(maxsize=20000)
def variants(word: str) -> tuple[str, ...]:
    """Confirmed spelling variants of ``word`` that exist in the dictionary.

    Imported lazily to avoid a circular import: the repository is the natural
    home for "is this in the dictionary", and it is what calls this module.
    """
    from backend.modules.vocabulary import repository

    entry = repository.lookup(word)
    if entry is None:
        return ()

    confirmed = []
    for candidate in candidates(word):
        other = repository.lookup(candidate)
        if other is not None and same_word(entry["translation"], other["translation"]):
            confirmed.append(candidate)
    return tuple(sorted(confirmed))


def clear_cache() -> None:
    variants.cache_clear()
