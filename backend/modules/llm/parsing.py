"""Getting JSON back out of a chat reply.

Shared by every worker, because they all hit the same three things: models wrap
JSON in code fences, prefix it with a sentence however firmly they were told not
to, and — with a reasoning model — sometimes emit a chain of thought around it.

A failure here raises :class:`ItemFailed` carrying the raw reply, so the reply is
stored with the failed item instead of being lost. Diagnosing "no JSON found"
without the text that contained no JSON means reproducing the call by hand,
which is exactly what the job record exists to avoid.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# Reasoning models sometimes leave the chain of thought in the message body.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class ItemFailed(Exception):
    """A job item failed in a way worth recording verbatim."""

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


def json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a reply, or raise :class:`ItemFailed`."""
    candidate = _THINK.sub("", text).strip()

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost braces: enough to survive a preamble, and
        # enough to survive a trailing "希望对你有帮助！".
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ItemFailed(
                "回复里没有找到 JSON 对象" if candidate else "回复是空的",
                raw=text,
            ) from None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ItemFailed(f"JSON 解析失败：{exc}", raw=text) from None

    if not isinstance(parsed, dict):
        raise ItemFailed("回复的 JSON 不是对象", raw=text)
    return parsed
