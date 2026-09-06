"""Lemmatisation with part-of-speech disambiguation.

This is the one piece of genuine language processing in the project, and the
reason the backend is written in Python at all.

**Why part-of-speech tagging is not optional.** The forms that matter most are
ambiguous on their own:

    He *left* the book        -> leave
    Turn *left*               -> left (direction)
    She *saw* the film        -> see
    Cut it with a *saw*       -> saw (tool)
    The *leaves* fell         -> leaf
    He *leaves* tomorrow      -> leave

All six are high-frequency words a learner will meet constantly. Resolving them
by spelling alone gets them wrong, and a wrong headword poisons everything
downstream: the review card shows a sentence that has nothing to do with the
word being reviewed.

**When this runs.** At article ingest time, once, and never again. Results are
stored per token, so reading, tapping a word and marking it unknown involve no
language processing whatsoever — which is why a weak ARM box is perfectly
adequate to serve the app.

Three sources are combined, in order of authority:
1. spaCy's tagger decides the part of speech.
2. lemminflect resolves the lemma for that word and tag — more accurate than
   rule-based lemmatisation on irregular forms.
3. The dictionary's own inflection table cross-checks the result and catches
   forms the first two disagree on.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import get_logger

log = get_logger("vocabulary.analyzer")

_MODEL_NAME = "en_core_web_sm"
_nlp: Any = None
_load_lock = threading.Lock()

# Parts of speech that carry vocabulary weight. Function words (determiners,
# prepositions, pronouns) are never study targets, and punctuation and numbers
# are not words at all.
CONTENT_POS = {"NOUN", "VERB", "ADJ", "ADV", "PROPN"}


@dataclass
class TokenAnalysis:
    """One word occurrence, fully resolved.

    ``char_start``/``char_end`` are offsets into the original text, which is what
    lets a client highlight the exact word the user tapped without re-parsing
    anything.
    """

    index: int
    text: str
    lemma: str
    pos: str
    tag: str
    char_start: int
    char_end: int
    is_word: bool
    is_proper_noun: bool
    is_content: bool
    in_dictionary: bool = False
    headword: str | None = None
    tags: str | None = None
    bnc: int | None = None
    frq: int | None = None
    translation: str | None = None
    lemma_source: str = "spacy"


@dataclass
class SentenceAnalysis:
    index: int
    text: str
    char_start: int
    char_end: int
    tokens: list[TokenAnalysis] = field(default_factory=list)


def _load_model() -> Any:
    """Load spaCy once, on first use.

    Lazily rather than at import so that starting the service, running a
    migration or opening the admin console does not pay the model load cost.
    """
    global _nlp
    if _nlp is not None:
        return _nlp

    with _load_lock:
        if _nlp is not None:
            return _nlp

        import spacy  # imported here to keep startup free of it

        try:
            # The parser is only needed for sentence boundaries; the senter
            # component does that far more cheaply.
            nlp = spacy.load(_MODEL_NAME, exclude=["parser", "ner"])
            nlp.enable_pipe("senter")
        except (OSError, ValueError):
            log.exception(
                "model.load.failed",
                f"无法加载语言模型 {_MODEL_NAME}，请先执行模型下载",
                model=_MODEL_NAME,
            )
            raise

        _nlp = nlp
        log.info("model.loaded", f"已加载语言模型 {_MODEL_NAME}", model=_MODEL_NAME)
        return _nlp


def warm() -> None:
    """Preload the model. Wired to the module's startup hook."""
    try:
        _load_model()
    except Exception:  # noqa: BLE001 - a missing model must not stop the service
        log.warning(
            "model.warm.skipped",
            "语言模型未就绪，词汇分析功能暂不可用，其余功能不受影响",
        )


# Parts of speech lemminflect accepts. Calling it with anything else prints a
# warning to stdout and returns nothing.
_LEMMINFLECT_POS = {"ADJ", "ADV", "NOUN", "PROPN", "VERB", "AUX"}


def _lemminflect_candidates(word: str, upos: str) -> tuple[str, ...]:
    """All lemmas lemminflect considers possible for ``word`` as ``upos``."""
    if upos not in _LEMMINFLECT_POS:
        return ()
    try:
        from lemminflect import getLemma
    except ImportError:  # pragma: no cover
        return ()

    for candidate in (word, word.lower()):
        lemmas = getLemma(candidate, upos=upos, lemmatize_oov=True)
        if lemmas:
            return tuple(lemma.lower() for lemma in lemmas)
    return ()


def resolve_lemma(token: Any) -> tuple[str, str]:
    """Decide the lemma for one token. Returns ``(lemma, source)``.

    The two tools fail in different ways, and combining them is what gets the
    ambiguous forms right:

    * lemminflect knows irregular morphology thoroughly but sees only the word
      and its tag, so for an ambiguous form it returns *several* candidates in
      an order that is often wrong — ``leaves`` as a noun yields
      ``('leave', 'leaf')``, and the correct answer is second.
    * spaCy saw the whole sentence, so when its answer is among those
      candidates it is the better-informed choice.

    Hence: prefer spaCy's lemma when both tools consider it possible, and fall
    back to lemminflect when spaCy produced something morphologically wrong.

    The fallback only applies when spaCy produced something that is *not a word*.
    With ``lemmatize_oov`` on, lemminflect will strip a suffix that was never
    there — ``other`` as an adjective comes back as ``('oth',)`` because ``-er``
    looks like a comparative, and ``oth`` happens to be in ECDICT as an
    abbreviation, so merely checking that a candidate exists is not enough. It
    cost four false out-of-syllabus hits across sixteen drafts.

    So the order is: agree if both accept it; otherwise keep spaCy's answer if it
    is a real headword, because spaCy read the sentence and lemminflect only saw
    a tag; only when spaCy's answer is not in the dictionary at all does the
    morphology table get to decide.
    """
    from backend.modules.vocabulary import repository

    spacy_lemma = (token.lemma_ or token.text).lower()

    if not token.is_alpha:
        return spacy_lemma, "spacy"

    candidates = _lemminflect_candidates(token.text, token.pos_)
    if not candidates:
        return spacy_lemma, "spacy"

    if spacy_lemma in candidates:
        return spacy_lemma, "agreed"

    if repository.lookup(spacy_lemma) is not None:
        return spacy_lemma, "spacy"

    # spaCy's answer is not a word at all — usually a rule-based mistake on an
    # irregular form. Now the morphology table is the better guess.
    for candidate in candidates:
        if repository.lookup(candidate) is not None:
            return candidate, "lemminflect"

    return spacy_lemma, "spacy"


# Bound morphemes that only ever appear attached to something else. A
# hyphenated compound whose first half is one of these ("socio-economic",
# "self-aware") must not have that half judged as a word in its own right —
# spaCy splits on the hyphen, and `socio` is in no syllabus because it is not a
# word. This list is the fallback; once the affix table is loaded, that is the
# better source.
COMBINING_FORMS = frozenset(
    """
    socio psycho physio bio geo neuro agro astro hydro thermo electro
    micro macro mini multi mono poly semi pseudo quasi proto retro
    anti auto co counter cross de dis ex extra hyper hypo il im in inter intra
    ir mid mis non over post pre pro re self sub super trans ultra un under
    """.split()
)


def hyphenated_spans(doc: Any) -> dict[int, tuple[int, int]]:
    """Map each token index to the hyphenated compound it belongs to.

    Returns ``{token index: (start, end)}`` with ``end`` exclusive. A compound is
    a run of alphabetic tokens joined by hyphens with no whitespace anywhere in
    between, which is exactly how ``socio-economic`` and ``well-being`` arrive
    after tokenisation — as three tokens that mean one word.
    """
    spans: dict[int, tuple[int, int]] = {}
    index = 0
    tokens = list(doc)

    while index < len(tokens):
        start = index
        end = index + 1
        # Extend while the pattern "word - word" continues unbroken.
        while (
            end + 1 < len(tokens)
            and tokens[end].text == "-"
            and not tokens[end - 1].whitespace_
            and not tokens[end].whitespace_
            and tokens[end - 1].is_alpha
            and tokens[end + 1].is_alpha
        ):
            end += 2
        if end > start + 1:
            for position in range(start, end):
                spans[position] = (start, end)
        index = end

    return spans


def compound_forms(doc: Any, span: tuple[int, int]) -> tuple[str, str]:
    """The hyphenated and the run-together spelling of one compound.

    Both are worth trying: dictionaries disagree about ``well-being`` versus
    ``wellbeing`` and ``socio-economic`` versus ``socioeconomic``.
    """
    start, end = span
    hyphenated = "".join(token.text for token in list(doc)[start:end]).lower()
    return hyphenated, hyphenated.replace("-", "")


def analyze(text: str) -> list[SentenceAnalysis]:
    """Split ``text`` into sentences and resolve every token.

    Dictionary enrichment is applied here too, so a caller gets one complete
    picture rather than having to walk the result a second time.
    """
    from backend.modules.vocabulary import repository

    nlp = _load_model()
    doc = nlp(text)

    sentences: list[SentenceAnalysis] = []
    token_index = 0

    for sent_index, sent in enumerate(doc.sents):
        analysis = SentenceAnalysis(
            index=sent_index,
            text=sent.text,
            char_start=sent.start_char,
            char_end=sent.end_char,
        )

        for token in sent:
            if token.is_space:
                continue

            is_word = token.is_alpha
            lemma, source = resolve_lemma(token)

            item = TokenAnalysis(
                index=token_index,
                text=token.text,
                lemma=lemma,
                pos=token.pos_,
                tag=token.tag_,
                char_start=token.idx,
                char_end=token.idx + len(token.text),
                is_word=is_word,
                # Proper nouns are shown and tappable, but never counted toward
                # the unknown-word rate and never enter the review queue —
                # skipping over an unfamiliar name is a reading skill in itself.
                is_proper_noun=token.pos_ == "PROPN",
                is_content=token.pos_ in CONTENT_POS and is_word,
                lemma_source=source,
            )

            if is_word:
                entry = repository.lookup(item.lemma)
                if entry is None:
                    # The lemmatiser produced something the dictionary does not
                    # know. The inflection table often resolves these.
                    fallback = repository.resolve_surface(token.text.lower())
                    if fallback:
                        item.lemma = fallback
                        item.lemma_source = "dictionary"
                        entry = repository.lookup(fallback)

                if entry is not None:
                    item.in_dictionary = True
                    item.headword = entry["headword"]
                    item.tags = entry["tags"]
                    item.bnc = entry["bnc"]
                    item.frq = entry["frq"]
                    item.translation = entry["translation"]

            analysis.tokens.append(item)
            token_index += 1

        sentences.append(analysis)

    return sentences


def summarise(sentences: list[SentenceAnalysis]) -> dict[str, Any]:
    """Aggregate counts for the analysis page.

    Deliberately does *not* compute an unknown-word rate: that needs the
    learner's ability estimate, which is P3. What it can say now is how much of
    the text the dictionary recognises, which is what P0 needs to verify.
    """
    tokens = [t for s in sentences for t in s.tokens]
    words = [t for t in tokens if t.is_word]
    content = [t for t in words if t.is_content]
    proper = [t for t in words if t.is_proper_noun]
    unknown = [t for t in content if not t.in_dictionary and not t.is_proper_noun]

    by_syllabus: dict[str, int] = {}
    for token in content:
        if not token.tags:
            continue
        for tag in token.tags.split():
            by_syllabus[tag] = by_syllabus.get(tag, 0) + 1

    return {
        "sentences": len(sentences),
        "tokens": len(tokens),
        "words": len(words),
        "content_words": len(content),
        "proper_nouns": len(proper),
        "distinct_lemmas": len({t.lemma for t in content}),
        "not_in_dictionary": len(unknown),
        "not_in_dictionary_samples": sorted({t.text for t in unknown})[:20],
        "by_syllabus": dict(sorted(by_syllabus.items(), key=lambda kv: -kv[1])),
    }
