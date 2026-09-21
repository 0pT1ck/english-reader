"""Which words get a sense set — 目标词 target words.

This is the one question that survived the coarse screen. P1b asked two things
at once: *which words matter* (this file) and *which of them are polysemous
enough to be worth paying a model for* (the screen, deleted in P10). The second
question turned out to be both unanswerable and unnecessary — unanswerable
because it was decided by counting commas in a Chinese gloss, which dismissed
65% of genuinely polysemous words including ``bank``, ``come``, ``do`` and
``positive``; unnecessary because senses now come from a dictionary, and a
dictionary either has the word or it does not.

The answer here has never depended on the screen: it comes straight from the
syllabus tags in ``dictionary.db``, which is why deleting the screen costs
nothing.
"""

from __future__ import annotations

from backend.core.db import get_connection

#: Syllabus levels a word must carry at least one of. Cumulative reading is
#: mandatory — ``the`` carries only ``zk gk`` and is obviously in scope, which
#: is 踩过的坑 §5.3: treating the tags as a single level measured CET-4 papers
#: as harder than postgraduate ones. ``ky`` (postgraduate) is in the list
#: because it always was — **copied verbatim from the screen rather than
#: retyped**, since dropping one tag here silently changes which words the
#: whole project considers in scope.
TARGET_TAGS = ("zk", "gk", "cet4", "cet6", "ky")


def target_words() -> list[tuple[str, str | None]]:
    """Every word in scope, with its dictionary translation.

    ``frq > 0`` drops entries the frequency list has never seen, which are
    overwhelmingly abbreviations and proper nouns that happen to carry a tag.
    """
    tag_clause = " OR ".join(f"tags LIKE '%{tag}%'" for tag in TARGET_TAGS)
    rows = get_connection("dictionary").execute(
        f"SELECT headword, translation FROM words"  # noqa: S608 - tags are a literal tuple
        f" WHERE ({tag_clause}) AND frq > 0 ORDER BY headword"
    ).fetchall()
    return [(row["headword"], row["translation"]) for row in rows]


def target_headwords() -> list[str]:
    """Just the words."""
    return [word for word, _ in target_words()]
