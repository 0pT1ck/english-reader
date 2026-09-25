"""Parsing Collins COBUILD into our sense inventory — P10.

柯林斯 Collins / 义项块 sense block / 词组义 phrase sense / 溯源 provenance

**Why a dictionary replaced the model.** Until P10 the inventory was written by
a model from nothing: 13,741 senses, 1.90 per word, and no way to answer "did we
miss one". P1c tried to fix that by handing the model an external checklist and
asking it to re-cluster; that failed on granularity (7.0 senses per word against
a target of 2.5) and was shelved. Collins settles it by not being a clustering
problem at all — the senses are already split, by lexicographers, ordered by
frequency: **3.60 per word, median 3**.

**What one entry looks like**::

    <span class="num">1.</span>
    <span class="st" tid="…">N-COUNT   可数名词</span>
    <span class="text_blue">（银行等的）账户</span>
    If you have an account with a bank …, you have an arrangement to leave …
    【搭配模式】：ADJ n   【语域标签】：FORMAL 正式
    <ul><li><p>example</p><p>例句译文</p></li></ul>

Which is, column for column, what ``senses`` wants: ordinal, pos, gloss_zh,
concept_en — plus two things the model never gave us, the register label and
real example sentences.

**The hard part is not parsing, it is the word/phrase line.** The user's
requirement for P10 is exact: *every single-word sense in, every phrase sense
out*, with phrases getting a phase of their own. Collins hangs phrase senses off
the main entry (``take into account`` is sense #21 of ``account``), so the split
has to be decided per block. Four criteria, in order — see :func:`classify`.

**This module never touches the database.** It turns HTML into dataclasses and
raises on anything it does not recognise; storing them is the importer's job.
That separation is what makes the criteria testable against known answers, which
is how the two misclassifications below were caught in the first place.
"""

from __future__ import annotations

import html as html_module
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Kind",
    "CollinsSense",
    "UnknownMarker",
    "classify",
    "parse_entry",
    "split_cross_ref",
    "normalise_gloss",
    "is_hollow",
]


class Kind(Enum):
    """What a numbered block in a Collins entry actually is."""

    WORD = "word"
    """A sense of the headword itself. **These are what P10 imports.**"""

    PHRASE = "phrase"
    """A sense of a multi-word expression filed under this headword.
    Excluded from P10 — phrases get their own phase."""

    CROSS_REF = "cross_ref"
    """``See also:`` or ``→see: aesthetic``. Not a sense at all."""


class UnknownMarker(Exception):
    """A part-of-speech marker the criteria have never seen.

    **Raised rather than guessed at, on purpose** — criterion 4. Collins has
    29,000 entries we do not import, so a change to the target word list can
    surface a marker that was never classified. An importer that silently files
    the unknown ones somewhere loses senses without a word of complaint, and
    that is exactly how the two mistakes recorded below got made.
    """


#: Markers that mean "this block is about a multi-word expression".
#:
#: Measured across the whole dictionary, not just our target words: 13 distinct
#: markers, 7,961 instances. **``PHRASAL`` does not contain the substring
#: ``PHRASE``**, which is how ``PHRASAL VERB`` (128 blocks) was misfiled as a
#: word sense on the first pass.
PHRASE_MARKER = re.compile(r"\bPHR|PHRASE|PHRASAL|\bV PHR\b", re.IGNORECASE)

#: Grammatical roots that mean "this block is about the headword itself".
#:
#: Collins composes markers (``N-COUNT; N-TITLE``, ``ADJ-GRADED``,
#: ``VERB: no passive``, ``COMB in ADJ``), so this matches the first token
#: rather than the whole string. Built by enumerating **all 141 markers in the
#: dictionary** and requiring zero left over — the first attempt at this list
#: omitted ``VERB`` and would have silently dropped 9,789 blocks.
WORD_ROOTS = frozenset("""
    N V VERB ADJ ADV PRON DET PREP CONJ NUM ORD QUANT AUX MODAL EXCLAM INTERJ
    COMB COLOUR FRACTION NEG QUEST SOUND PREDET PASSIVE LINK TITLE VOC MASS
    SING PLURAL PREFIX SUFFIX TO
""".split())
# ``TO`` is there for ``to inf`` — the infinitive marker, 5 blocks, all under
# ``to`` itself. **Criterion 4 found it**, on a rescan after the part-of-speech
# regex was fixed: the broken regex had never surfaced those blocks, so the
# first scan of "all 141 markers" was itself incomplete. The list is now 143,
# rebuilt with the parser's own extraction rather than an approximation of it.

#: An entry shorter than this is a stub: headword plus a frequency star and
#: nothing else. Twelve of our target words are like this (``spring``,
#: ``within``), which is why "has an entry" and "has content" are different
#: questions — the coverage figure that mixes them up reads 96.6% instead of
#: 96.4%.
HOLLOW_ENTRY_CHARS = 200

#: Below this, a block's English text is not a definition. Cross-reference-only
#: blocks (``→see: aesthetic``) fall here; so do a handful of real but very
#: short definitions (``A guy is a man.`` is 15 characters), which is why the
#: threshold is this low and why :func:`classify` checks the arrow explicitly
#: rather than trusting length alone.
MIN_DEFINITION_CHARS = 12

_BLOCK_SPLIT = re.compile(r'<span class="num">(\d+)\.</span>')
#: **Three shapes, and the whole span has to be captured to tell them apart.**
#: Usually ``ADV\t副词``; 2,026 times English only; and 83 times the Chinese is
#: in a nested ``<div>`` instead (``N-UNCOUNT<div …>不可数名词</div>``). An
#: earlier version stopped at the first ``<``, so those 83 never matched at all
#: and **the marker leaked into the English definition** — found by diffing the
#: parser against the throwaway probe rather than by reading the HTML.
_POS = re.compile(r'<span class="st"[^>]*>(.*?)</span>', re.DOTALL)
_POS_NESTED_ZH = re.compile(r"<div[^>]*>(.*?)</div>", re.DOTALL)
_GLOSS = re.compile(r'<span class="text_blue">(.*?)</span>', re.DOTALL)
_BOLD = re.compile(r"<b>(.*?)</b>", re.DOTALL)
#: Collins's bracketed labels. Six kinds exist; these are the two P10 keeps.
#: ``语域标签`` (FORMAL/BRIT/INFORMAL, 8,849) and ``STYLE标签``
#: (OLD-FASHIONED, 4,591) answer the same question — *what situation is this
#: used in* — so they are stored together. ``语法信息`` and ``语用信息`` are
#: prose about usage rather than labels, and are not stored.
_REGISTER = re.compile(r"【(?:语域标签|STYLE标签)】：([^<【]+)")
_PATTERN = re.compile(r"【搭配模式】：([^<【]+)")
_FIELD = re.compile(r"【FIELD标签】：([^<【]+)")
_WORD_GRAM = re.compile(r'<div\s+id="word_gram.*', re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
#: Collins appends "see also" pointers inside the definition text, and they
#: come in two shapes that must be told apart:
#:
#: * a whole block that is nothing but pointers — ``to add insult to
#:   injury→see: insult;`` — which is a cross-reference, not a sense;
#: * a real definition with a pointer stuck on the end — ``When a cake bakes…
#:   →see usage note at: …`` — which is a sense whose tail needs trimming.
#:
#: **Having a Chinese gloss is what separates them.** The first kind never has
#: one; the second always does. An earlier version only looked for a pointer at
#: the *start* of the text, so 60-odd pointer-only blocks were imported as
#: senses and turned up in the annotator's candidate list as things like
#: ``not be the end of the world→see: end; the world is your oyster→see:
#: oyster;`` — which is both wrong and a waste of prompt.
_CROSS_REF_TAIL = re.compile(r"(→|&rarr;)\s*see\b", re.IGNORECASE)
_EXAMPLE = re.compile(
    r"<li[^>]*>\s*<p>(.*?)</p>\s*(?:<p>(.*?)</p>)?", re.DOTALL
)


#: Tags that sit *inside* a word and must vanish without leaving a space.
#: Collins highlights the headword mid-inflection
#: (``<span class='text_blue'>account</span>s``), so replacing every tag with a
#: space splits the word: ``I had two account s with Natwest``. Definitions
#: never showed this because their tags already have spaces either side — only
#: the example sentences did, which is why it survived the first import.
_INLINE_TAGS = re.compile(
    r"</?(?:span|b|i|em|strong|a|sub|sup|font|u)\b[^>]*>", re.IGNORECASE
)


#: Collins mixes half- and full-width brackets inside one Chinese gloss —
#: ``承兑，认付（单据等）；认可(文件等)`` is a single entry. 5,649 glosses carry a
#: half-width bracket and 5,005 a full-width one, so this is the source's own
#: typesetting rather than a parsing artefact. Chinese text takes full-width
#: brackets, and normalising here means a re-import produces the same thing.
#: **Only the Chinese is touched** — the English definitions use half-width
#: brackets correctly and are left alone.
_GLOSS_BRACKETS = str.maketrans({"(": "（", ")": "）"})


def normalise_gloss(text: str) -> str:
    """Chinese gloss, with brackets made consistent."""
    return text.translate(_GLOSS_BRACKETS)


def split_cross_ref(text: str) -> tuple[str, bool]:
    """Definition without its trailing pointers, and whether any were found."""
    match = _CROSS_REF_TAIL.search(text)
    if not match:
        return text, False
    return text[:match.start()].strip(" .;,"), True


def _text(html: str) -> str:
    """Strip tags and collapse whitespace. Collins pads with tabs and nbsp.

    Inline tags are removed outright; everything else becomes a space, because
    a ``<br>`` or ``</p>`` really is a word boundary.
    """
    without_inline = _INLINE_TAGS.sub("", html)
    stripped = _TAGS.sub(" ", without_inline)
    # Entities last, so that an escaped `&lt;div&gt;` in the text cannot turn
    # into something the tag stripper would have eaten.
    unescaped = html_module.unescape(stripped).replace("\xa0", " ")
    return _WS.sub(" ", unescaped).strip()


#: Where the definition part of a block ends: the examples list, the grammar
#: box, or the next sense's container.
_CAPTION_END = (
    '<ul',
    '<div class="collins_en_cn"',
    '<div class="caption"',
)
_GRAMMAR_BOX = re.compile(r'<span\s*><div\s+id="word_gram')


def _caption(body: str) -> str:
    """The definition part of a block: marker, gloss, English definition.

    **Not ``body.split("</div>")[0]``**, which is what this was at first. That
    works only while nothing inside the caption contains a ``</div>`` — and 83
    blocks put the Chinese part of speech in a nested ``<div>``, so the caption
    got cut off before its own ``</span>`` and the marker vanished. The symptom
    was the marker turning up inside the English definition
    (``dash`` #12: ``concept_en == "N-UNCOUNT 不可数名词"``).

    Cutting at what *starts* the next part instead is robust to nesting.
    """
    cut = len(body)
    for marker in _CAPTION_END:
        index = body.find(marker)
        if 0 <= index < cut:
            cut = index
    box = _GRAMMAR_BOX.search(body)
    if box and box.start() < cut:
        cut = box.start()
    return body[:cut]


@dataclass(slots=True)
class CollinsSense:
    """One numbered block, parsed.

    ``ordinal`` is what Collins printed, and **it is not unique within an
    entry** — long entries restart numbering per section (``take`` has two
    blocks labelled 1). ``block_index`` is the position in document order and is
    what provenance keys on; see P10 §5 for why identity must not be derived
    from anything the dictionary prints.
    """

    headword: str
    block_index: int
    ordinal: int
    kind: Kind
    pos: str = ""
    """The raw marker, e.g. ``N-COUNT``."""
    pos_zh: str = ""
    """Collins's own Chinese for it, e.g. 可数名词. **Show this, not the
    English** — a CET learner reading ``N-COUNT`` on screen learns nothing."""
    concept_en: str = ""
    gloss_zh: str = ""
    register: str = ""
    """``FORMAL 正式`` / ``BRIT 英`` / ``INFORMAL 非正式``. Stored, not used yet
    (P10 §6): it turns "is this general written English" from a question for a
    model into a lookup."""
    pattern: str = ""
    """``ADJ n``, ``ADV after v`` — Collins's collocation pattern."""
    subject: str = ""
    """Subject field (Collins's 【FIELD标签】), e.g. 医学 or 法律. Stored, not
    used yet. **Not named ``field``** — that shadows ``dataclasses.field``
    inside the class body and breaks the very next line."""
    examples: list[tuple[str, str]] = field(default_factory=list)
    """``(english, chinese)`` pairs. ``sense_examples`` has been empty for a
    year; these fill it."""
    bolds: list[str] = field(default_factory=list)
    """The block's emboldened runs, **each kept separate**.

    P11 needs them to tell which phrase a block is about, and separateness is
    the whole point: Collins emboldens a phrase with a variable in the middle in
    pieces (``take`` … ``into account``), so joining them first is how
    ``on one's account`` gets matched to ``on account`` 赊账 (P11 §13).
    :func:`classify` only ever asks how many there are, so this changes nothing
    for P10."""


def is_hollow(html: str) -> bool:
    """Entry exists but carries no content. See :data:`HOLLOW_ENTRY_CHARS`."""
    return len(html) < HOLLOW_ENTRY_CHARS


def _squash(key: str) -> str:
    return key.replace(" ", "").replace("-", "")


def squash_index(entries: Mapping[str, str]) -> dict[str, list[str]]:
    """Keys grouped by their spelling with spaces and hyphens removed."""
    index: dict[str, list[str]] = {}
    for key in entries:
        index.setdefault(_squash(key), []).append(key)
    return index


def entry_key(word: str, entries: Mapping[str, str],
              index: Mapping[str, list[str]]) -> str | None:
    """Which dictionary key holds ``word``'s entry.

    **Usually the word itself.** When it is not there, Collins may write it
    with a space or a hyphen where the corpus does not — ``percent`` is under
    ``per cent`` (111 tokens in the corpus), ``northeast`` under ``north-east``.
    Measured over the syllabus: 8 words, all genuine. Only an **unambiguous**
    match counts; two keys squashing to the same string is a question, not an
    answer. Senses are still stored under ``word`` — that is what the tokens
    carry — so the import and its reconciliation (verify_phase10 A1) must both
    come through here, or they would disagree about what the dictionary says.
    """
    if word in entries:
        return word
    # Affix entries (``-ware``, ``-ish``) squash onto real words — ``ware``
    # 器皿 would otherwise get the suffix's sense. A leading or trailing hyphen
    # is never a spelling variant.
    candidates = [k for k in index.get(_squash(word), ())
                  if k != word and not k.startswith("-") and not k.endswith("-")]
    return candidates[0] if len(candidates) == 1 else None


def classify(headword: str, pos: str, concept_en: str, gloss_zh: str,
             bolds: list[str], *, had_cross_ref: bool = False) -> Kind:
    """Which of the three a block is. **The four criteria, in order.**

    1. ``See also:`` / empty marker with no definition → cross-reference.
    2. A phrase marker → phrase.
    3. ``CONVENTION`` → decided by what is emboldened, because that marker
       covers both ``hello`` (the word itself) and ``after you`` (a phrase).
       55 of our 263 are the word itself.
    4. A known grammatical root → word. **Anything else raises**, rather than
       being filed by resemblance.

    A criterion that was tried and rejected: *"one bolded word means a word
    sense, several mean a phrase"*. Measured against the above on all 30,965
    blocks it disagrees on **17.3%, and is wrong in both directions** —
    ``ability``'s own sense bolds ``ability to`` (a collocation, not a phrase),
    while ``on someone's account`` bolds only ``account`` because the middle is
    a variable. Collins emboldens *how a sense is used*, which is a different
    question from *what kind of thing this is*; the marker answers the second
    one directly.
    """
    marker = (pos or "").strip()

    if marker.startswith("See also"):
        return Kind.CROSS_REF

    if not marker:
        # Collins does not label every block. A block with a real definition is
        # a real sense (736 of them, including `attach` #1 and `cause` #1) —
        # **these were discarded as empty on the first pass.**
        #
        # The rest are pointers, and **length does not separate them** — that
        # was the second attempt and it let `world` keep a sense reading
        # "not be the end of the world" (27 characters, no definition in it at
        # all). What separates them is the gloss: Collins writes a Chinese
        # gloss for every real sense, and never for a pointer block.
        if had_cross_ref and not gloss_zh:
            return Kind.CROSS_REF
        if len(concept_en) < MIN_DEFINITION_CHARS and not gloss_zh:
            return Kind.CROSS_REF
        return Kind.WORD

    if PHRASE_MARKER.search(marker):
        return Kind.PHRASE

    if marker == "CONVENTION":
        candidates = [b for b in bolds if b]
        if not candidates:
            return Kind.PHRASE
        longest = max(candidates, key=len)
        own = longest.lower().strip(" ',.").replace("&nbsp;", "")
        return Kind.WORD if (len(longest.split()) == 1
                             and own == headword.lower()) else Kind.PHRASE

    root = re.split(r"[-;: ]", marker)[0].upper()
    if root in WORD_ROOTS:
        return Kind.WORD

    raise UnknownMarker(
        f"{headword!r}: 没见过的词性标记 {marker!r}。"
        f"照 P10 §4 判据 4，这里报错而不是猜——"
        f"确认它是单词义还是词组义之后，把词根加进 WORD_ROOTS 或 PHRASE_MARKER。"
    )


def parse_entry(headword: str, html: str) -> list[CollinsSense]:
    """Every numbered block of one entry, classified but not filtered.

    Returns phrases and cross-references too; **deciding what to keep is the
    caller's job**, so that "how many did we drop, and why" stays answerable
    rather than being lost inside this function.
    """
    headword = (headword or "").strip().lower()
    if is_hollow(html):
        return []

    parts = _BLOCK_SPLIT.split(html)[1:]
    out: list[CollinsSense] = []

    for index, (ordinal, body) in enumerate(zip(parts[0::2], parts[1::2])):
        caption = _caption(body)

        pos = pos_zh = ""
        pos_match = _POS.search(caption)
        if pos_match:
            raw = pos_match.group(1)
            nested = _POS_NESTED_ZH.search(raw)
            if nested:
                pos, pos_zh = _text(raw[:nested.start()]), _text(nested.group(1))
            else:
                head, _, tail = raw.partition("\t")
                pos, pos_zh = _text(head), _text(tail)

        gloss_match = _GLOSS.search(caption)
        gloss_zh = normalise_gloss(_text(gloss_match.group(1))) if gloss_match else ""

        bolds = [_text(b) for b in _BOLD.findall(caption)]

        register_match = _REGISTER.search(body)
        pattern_match = _PATTERN.search(body)
        field_match = _FIELD.search(body)

        # The English definition is what is left of the caption once Collins's
        # own furniture is removed.
        english = caption
        if pos_match:
            english = english.replace(pos_match.group(0), " ")
        if gloss_match:
            english = english.replace(gloss_match.group(0), " ")
        english = _WORD_GRAM.sub(" ", english)
        concept_en, had_cross_ref = split_cross_ref(_text(english))
        # The nested-div part of speech leaves its Chinese behind when the
        # marker regex matched only the English half; belt and braces.
        if pos_zh and concept_en.startswith(pos_zh):
            concept_en = concept_en[len(pos_zh):].strip()

        kind = classify(headword, pos, concept_en, gloss_zh, bolds,
                        had_cross_ref=had_cross_ref)

        examples: list[tuple[str, str]] = []
        for en, zh in _EXAMPLE.findall(body):
            en_text, zh_text = _text(en), _text(zh or "")
            if en_text:
                examples.append((en_text, zh_text))

        out.append(CollinsSense(
            headword=headword,
            block_index=index,
            ordinal=int(ordinal),
            kind=kind,
            pos=pos,
            pos_zh=pos_zh,
            concept_en=concept_en,
            gloss_zh=gloss_zh,
            register=_text(register_match.group(1)) if register_match else "",
            pattern=_text(pattern_match.group(1)) if pattern_match else "",
            subject=_text(field_match.group(1)) if field_match else "",
            examples=examples,
            bolds=bolds,
        ))

    return out
