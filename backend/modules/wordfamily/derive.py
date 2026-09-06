"""Find derivations by rule, before spending anything on a model.

The method is deliberately dumb and therefore trustworthy: strip a known affix,
undo the spelling change it caused, and accept the result only if it is a real
headword that is *more common* than the word we started from. A root that is
rarer than its derivation is a coincidence, not a root — that single test is
what stops ``mock`` being derived from ``moc`` and ``witty`` from ``witt``.

Grades follow the design's three tiers. A suffix decides the grade because the
suffix is what determines how much meaning was added:

* **A** — the derivation is grammar, not vocabulary. ``careful`` → ``carefully``
  adds nothing a reader has to learn. These never count as new words.
* **B** — inferable but specialised. ``nation`` → ``nationality``.
* **C** — the meaning has drifted; the breakdown would mislead. The rules here
  never assign C, because a rule cannot see meaning. C is what the model pass
  and the prefix defaults produce.

Prefixes default to C for the same reason: ``depart`` is not ``part`` and
``recover`` is not ``cover``, while ``unused`` plainly is ``used``. Only the
prefixes that cannot change meaning — negation and repetition — are graded A.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.modules.vocabulary import repository

# suffix -> (grade, what it makes). Longest first: -ation must be tried before
# -tion, and -ically before -ly.
SUFFIX_RULES: tuple[tuple[str, str, str], ...] = (
    ("ically", "A", "adverb"),
    ("ation", "B", "noun"),
    ("ition", "B", "noun"),
    ("ution", "B", "noun"),
    ("ness", "A", "noun"),
    ("less", "A", "adjective"),
    ("ment", "B", "noun"),
    ("ship", "B", "noun"),
    ("hood", "B", "noun"),
    ("able", "B", "adjective"),
    ("ible", "B", "adjective"),
    ("ance", "B", "noun"),
    ("ence", "B", "noun"),
    ("sion", "B", "noun"),
    ("tion", "B", "noun"),
    ("ical", "B", "adjective"),
    ("ious", "B", "adjective"),
    ("eous", "B", "adjective"),
    ("ally", "A", "adverb"),
    ("ify", "B", "verb"),
    ("ise", "B", "verb"),
    ("ize", "B", "verb"),
    ("ism", "B", "noun"),
    ("ist", "B", "noun"),
    ("ity", "B", "noun"),
    ("ive", "B", "adjective"),
    ("ous", "B", "adjective"),
    ("ful", "A", "adjective"),
    ("ary", "B", "adjective"),
    ("ory", "B", "adjective"),
    ("age", "B", "noun"),
    ("ant", "B", "noun"),
    ("ent", "B", "noun"),
    ("ial", "B", "adjective"),
    ("ian", "B", "noun"),
    ("ure", "B", "noun"),
    ("dom", "B", "noun"),
    ("ish", "B", "adjective"),
    # Tried after every longer -ion variant, so `execution` gets stem `execut`
    # (-> execute) rather than the useless `exec` that -ution produces.
    ("ion", "B", "noun"),
    ("ly", "A", "adverb"),
    ("al", "B", "adjective"),
    ("er", "A", "noun"),
    ("or", "A", "noun"),
    ("en", "B", "verb"),
    ("th", "B", "noun"),
    ("y", "B", "adjective"),
)

# Prefixes that cannot change what the root means — only negate or repeat it.
TRANSPARENT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("un", "A"), ("non", "A"), ("re", "A"), ("dis", "B"), ("mis", "B"),
    ("over", "B"), ("under", "B"), ("pre", "B"), ("post", "B"), ("anti", "B"),
    ("inter", "C"), ("sub", "C"), ("super", "B"), ("trans", "C"), ("ex", "C"),
    ("de", "C"), ("in", "C"), ("im", "C"), ("il", "C"), ("ir", "C"),
    ("co", "C"), ("counter", "B"), ("self", "A"), ("multi", "B"), ("semi", "B"),
)

MIN_ROOT_LENGTH = 3

# What the root of each suffix has to be, for the suffixes where a coincidence
# is otherwise likely. ``-ly`` attaches to adjectives, so ``early`` is not
# ``ear + -ly``; agentive ``-er`` attaches to verbs, so ``matter`` is not
# ``mat + -er``. The B-grade suffixes are left unconstrained: they are much
# less prone to accidental matches, and a wrong one costs a third of a target
# slot rather than a claim that a word is not new.
ROOT_POS_REQUIRED: dict[str, frozenset[str]] = {
    "ly": frozenset({"adj"}),
    "ally": frozenset({"adj", "noun"}),
    "ically": frozenset({"adj", "noun"}),
    "ness": frozenset({"adj"}),
    "er": frozenset({"verb"}),
    "or": frozenset({"verb"}),
    "ful": frozenset({"noun", "verb"}),
    "less": frozenset({"noun", "verb"}),
    "en": frozenset({"adj", "noun"}),
}


@dataclass(frozen=True)
class Derivation:
    member: str
    root: str
    affix: str
    affix_kind: str          # 'prefix' | 'suffix'
    grade: str               # 'A' | 'B' | 'C'
    source: str = "rule"


def _stem_candidates(stem: str) -> list[str]:
    """Undo the spelling changes English makes when a suffix is attached."""
    out = [stem]
    if stem.endswith("i"):
        out.append(stem[:-1] + "y")       # happi(ness) -> happy
    out.append(stem + "e")                # natur(al) -> nature, execut(ion) -> execute
    out.append(stem + "y")                # charit(able) -> charity
    out.append(stem + "ate")              # particip(ant) -> participate
    if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
        out.append(stem[:-1])             # runn(ing) -> run
    if stem.endswith("s"):
        out.append(stem[:-1] + "d")       # defens(ive) -> defend
    return out


# Syllabus tiers from most to least elementary. A root should be no harder than
# what is built on it.
TIER_ORDER = ("zk", "gk", "cet4", "cet6", "ky", "ielts", "toefl", "gre")
UNTAGGED = len(TIER_ORDER)

# Secondary-school vocabulary. A word at this level needs no breakdown.
ELEMENTARY_TIERS = frozenset({"zk", "gk"})

# How much rarer than its own derivation a root may be. Two words of the same
# family sit within a factor of two or three of each other (nation 412 /
# national 231); a coincidence does not (mother 229 / moth 8580).
MAX_RARITY_RATIO = 3.0


def _tier_rank(headword: str) -> int:
    tags = repository.tags_of(headword)
    ranks = [i for i, tier in enumerate(TIER_ORDER) if tier in tags]
    return min(ranks) if ranks else UNTAGGED


def _frequency(headword: str) -> int:
    """Lower is more common. Missing frequency sorts last."""
    entry = repository.lookup(headword)
    if entry is None:
        return 10**9
    for key in ("frq", "bnc"):
        value = entry[key] or 0
        if value:
            return int(value)
    # Tagged but unranked words are common enough to have made a syllabus.
    return 50_000 if repository.tags_of(headword) else 10**9


def _plausible_root(candidate: str, member: str) -> bool:
    """Whether ``candidate`` is elementary enough to be ``member``'s root.

    Raw frequency alone does not work: adverbs outrank the adjectives they come
    from (``carefully`` is more common than ``careful``), so requiring the root
    to be the commoner word throws away the most transparent derivations there
    are. Two tests are used instead.

    Either the root sits no deeper in the syllabus than its derivation — which
    is what makes ``careful`` (中考) a plausible root for the untagged
    ``carefully`` — or it is a syllabus word of comparable frequency. The second
    test is what rejects ``reply`` → ``rep``: ``rep`` is in no syllabus at all,
    so no amount of similar frequency makes it a root. The tier test rejects
    ``mother`` → ``moth``, where the supposed root is both rarer and harder.
    """
    if _tier_rank(candidate) <= _tier_rank(member):
        return True
    if not repository.tags_of(candidate):
        return False
    member_frequency = _frequency(member)
    return _frequency(candidate) <= member_frequency * MAX_RARITY_RATIO


def _best_root(
    candidates: list[str], member: str, required_pos: frozenset[str] | None = None
) -> str | None:
    """Pick the most plausible real headword among the stem candidates."""
    best: tuple[int, str] | None = None

    for candidate in candidates:
        if len(candidate) < MIN_ROOT_LENGTH or candidate == member:
            continue
        if repository.lookup(candidate) is None:
            continue
        if required_pos is not None:
            pos = repository.parts_of_speech(candidate)
            # An entry with no usable markers is given the benefit of the doubt;
            # rejecting it would lose real families for a data gap.
            if pos and not (pos & required_pos):
                continue
        if not _plausible_root(candidate, member):
            continue
        rank = _frequency(candidate)
        if best is None or rank < best[0]:
            best = (rank, candidate)

    return best[1] if best else None


def by_suffix(word: str) -> Derivation | None:
    for suffix, grade, _pos in SUFFIX_RULES:
        if not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        if len(stem) < MIN_ROOT_LENGTH:
            continue

        # Agentive -er/-or is where nearly all the remaining coincidences live:
        # matter/mat, letter/let, corner/corn, flower/flow — short common nouns
        # that look like a short common verb plus the suffix, and pass every
        # other test because ECDICT gives those roots a verb sense. They are all
        # secondary-school words, where an agentive breakdown would never be
        # shown anyway, so that is where the rule stops looking.
        if suffix in ("er", "or") and (
            len(stem) < 4 or repository.tags_of(word) & ELEMENTARY_TIERS
        ):
            continue

        candidates = _stem_candidates(stem)
        if suffix == "ly":
            # -ly after a consonant + l swallows the root's final e:
            # simple -> simply, gentle -> gently, possible -> possibly.
            candidates.append(word[:-1] + "e")

        root = _best_root(candidates, word, ROOT_POS_REQUIRED.get(suffix))
        if root:
            return Derivation(word, root, f"-{suffix}", "suffix", grade)
    return None


def by_prefix(word: str) -> Derivation | None:
    for prefix, grade in TRANSPARENT_PREFIXES:
        if not word.startswith(prefix):
            continue
        stem = word[len(prefix):]
        # A hyphen after the prefix is a strong signal on its own (self-aware).
        stem = stem.lstrip("-")
        if len(stem) < MIN_ROOT_LENGTH:
            continue
        root = _best_root([stem], word)
        if root:
            return Derivation(word, root, f"{prefix}-", "prefix", grade)
    return None


def analyse(word: str) -> Derivation | None:
    """Best rule-based breakdown for one word, or ``None``.

    Suffixes are tried first: they are far more reliable, and a word carrying
    both (``unhappiness``) is better explained by the suffix, whose root the
    prefix pass will then decompose in its own right.

    Elementary words are analysed too, even though the reader knows them. The
    breakdown of ``nationality`` runs through ``national``, and a family whose
    middle link is missing cannot be walked; the same words also anchor the
    root-level view of a family. What remains of the coincidental matches at
    that level (``corner`` from ``corn``) is what the model review pass demotes.
    """
    return by_suffix(word.lower()) or by_prefix(word.lower())


def analyse_all(words: list[str]) -> list[Derivation]:
    found = []
    for word in words:
        derivation = analyse(word)
        if derivation is not None:
            found.append(derivation)
    return found
