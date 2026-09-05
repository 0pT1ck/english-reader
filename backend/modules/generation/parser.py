"""Parse what comes back from a chat window.

Marked plain text, not JSON. Chat interfaces habitually wrap output in markdown
fences, prepend "Sure, here's the article you asked for:", and mangle escaping
inside long strings with quotes and newlines. Any one of those breaks a JSON
parse, and in the manual workflow a failed parse costs the user another
copy-paste round trip.

Marked text survives all three: the parser looks for its markers and ignores
everything around them.

The parser is deliberately forgiving in specific, enumerated ways — each
tolerance below corresponds to something chat models actually do — while still
refusing input it cannot read, rather than silently storing a fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.core.logging import get_logger

log = get_logger("generation.parser")

START = re.compile(r"={2,}\s*ARTICLE(?:\s+\d+)?\s*={2,}", re.I)
END = re.compile(r"={2,}\s*END\s*={2,}", re.I)
TITLE = re.compile(r"^\s*(?:TITLE|标题)\s*[:：]\s*(.+?)\s*$", re.I | re.M)


@dataclass
class ParsedArticle:
    title: str
    body: str
    warnings: list[str]

    @property
    def word_count(self) -> int:
        return len(self.body.split())


class ParseError(Exception):
    """The response could not be read as an article."""


def _strip_code_fences(text: str) -> str:
    """Remove markdown fences that wrap the whole reply.

    Only the outermost pair, and only when they enclose everything — a fence in
    the middle of the text is content, not packaging.
    """
    stripped = text.strip()
    if stripped.startswith("```") and stripped.rstrip().endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1])
    return text


def parse(response: str) -> ParsedArticle:
    """Extract one article from a chat reply.

    Raises ``ParseError`` when there is no usable article. Warnings are for
    things that were recovered from — worth showing the user so they can tighten
    the prompt, but not worth rejecting the article over.
    """
    warnings: list[str] = []
    text = _strip_code_fences(response)

    start = START.search(text)
    if start:
        body_region = text[start.end() :]
    else:
        # The model skipped the opening marker. Common enough that rejecting
        # outright would waste a round trip; the closing marker or the title
        # line usually still anchors the content.
        warnings.append("没有找到 ===ARTICLE=== 起始标记，已尝试从正文推断")
        body_region = text

    end = END.search(body_region)
    if end:
        body_region = body_region[: end.start()]
    else:
        warnings.append("没有找到 ===END=== 结束标记，已取到文本末尾")

    title_match = TITLE.search(body_region)
    if title_match:
        title = title_match.group(1).strip()
        body_region = body_region[: title_match.start()] + body_region[title_match.end() :]
    else:
        title = ""
        warnings.append("没有找到 TITLE 行")

    # Chat replies often carry a leading sentence of commentary before the
    # article proper. Anything before the first blank-line-separated block of
    # real prose is dropped when it looks like commentary rather than content.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body_region) if p.strip()]
    if paragraphs and _looks_like_commentary(paragraphs[0]):
        warnings.append(f"忽略了开头的说明文字：{paragraphs[0][:40]!r}")
        paragraphs = paragraphs[1:]
    if paragraphs and _looks_like_commentary(paragraphs[-1]):
        warnings.append(f"忽略了结尾的说明文字：{paragraphs[-1][:40]!r}")
        paragraphs = paragraphs[:-1]

    body = "\n\n".join(re.sub(r"\s+", " ", p).strip() for p in paragraphs)

    if not body:
        raise ParseError("没有解析出任何正文内容")
    if len(body.split()) < 80:
        raise ParseError(f"正文过短（{len(body.split())} 词），可能没有完整复制")

    if not title:
        # Fall back to the first sentence so the draft is at least identifiable
        # in a list.
        title = body.split(".")[0][:60]

    log.debug(
        "response.parsed",
        f"解析出 {len(body.split())} 词",
        words=len(body.split()),
        paragraphs=len(paragraphs),
        warnings=len(warnings),
    )
    return ParsedArticle(title=title, body=body, warnings=warnings)


def _looks_like_commentary(paragraph: str) -> bool:
    """Whether a paragraph is the model talking rather than the article.

    Two signals, both cheap and both specific to how chat models frame replies:
    Chinese characters (the article is entirely English), and the short
    conversational openers they habitually add.
    """
    if re.search(r"[一-龥]", paragraph):
        return True

    lowered = paragraph.lower().strip()
    openers = (
        "sure,", "here's", "here is", "certainly", "of course",
        "i've written", "i have written", "below is", "hope this",
        "let me know", "feel free",
    )
    return len(paragraph.split()) < 25 and lowered.startswith(openers)
