"""The actual HTTP call to a model, plus retries and token accounting.

Two shapes, one return type. Callers get :class:`Completion` and never learn
which vendor answered — that is what lets a batch job be re-pointed at a
different provider without touching the worker that uses it.

Retries cover exactly the failures that are worth retrying: rate limits, server
errors, and network timeouts. A 400 means the request is wrong and will be
wrong again, so it fails immediately rather than burning three attempts and the
user's patience.

**No key ever reaches a log record.** The key goes into a header and nowhere
else; errors are logged with the provider id and the status code.
"""

from __future__ import annotations

import contextvars
import json
import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from backend.core import runtime_config
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.modules.llm import secrets_store
from backend.modules.llm.providers import Provider

log = get_logger("llm.client")

# Which way the chain of thought is set for the work currently running in this
# thread. Set by the batch runner around each item (see ``llm.jobs``); unset
# everywhere else, which means "do not send the field at all".
_thinking: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "llm_thinking", default=None
)


def current_thinking() -> bool | None:
    return _thinking.get()


@contextmanager
def thinking(value: bool | None):
    """Apply a thinking setting to every completion made inside this block."""
    token = _thinking.set(value)
    try:
        yield
    finally:
        _thinking.reset(token)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Ceiling for the automatic budget escalation below. High enough for a reasoning
# model to think its way through a full batch, low enough that a model stuck in
# a loop stops costing money.
MAX_OUTPUT_TOKENS = 16000


class LLMError(RuntimeError):
    """A call failed in a way the caller has to handle (and the job records)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    raw: dict[str, Any]


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

_rate_lock = threading.Lock()
_recent_calls: deque[float] = deque()


def _await_slot() -> None:
    """Hold back until the configured requests-per-minute allows another call.

    Needed because speed and quota pull in opposite directions: a fast model
    finishes a call in two seconds, so three worker threads happily issue ninety
    requests a minute — and a relay that allows twenty answers with 429s for the
    other seventy. Backing off after the fact wastes the call that was refused;
    spacing them out beforehand does not.

    Zero means unlimited, which is right for a provider you talk to directly.
    """
    limit = int(runtime_config.get("llm_requests_per_minute"))
    if limit <= 0:
        return

    while True:
        with _rate_lock:
            now = time.monotonic()
            while _recent_calls and now - _recent_calls[0] >= 60.0:
                _recent_calls.popleft()
            if len(_recent_calls) < limit:
                _recent_calls.append(now)
                return
            wait = 60.0 - (now - _recent_calls[0]) + 0.05
        time.sleep(max(0.05, wait))


def _retry_after(response: httpx.Response) -> float | None:
    """How long the server asked us to wait, if it said.

    Checked in both places providers put it: the standard header, and — as this
    relay does — a field inside the JSON error body.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict) and "retryAfterSeconds" in data:
            try:
                return float(data["retryAfterSeconds"])
            except (TypeError, ValueError):
                return None
    return None


def _client(timeout: float) -> httpx.Client:
    # Proxy comes from settings, never hardcoded — on this machine it is the
    # local v2rayN mixed port, on a server it is usually absent.
    proxy = get_settings().proxy_url or None
    return httpx.Client(timeout=timeout, proxy=proxy, follow_redirects=True)


def _openai_request(provider: Provider, key: str, messages: list[dict[str, str]],
                    *, max_tokens: int, temperature: float,
                    json_mode: bool, stream: bool = False,
                    thinking: bool | None = None) -> tuple[str, dict, dict]:
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Only sent when someone asked for it. Omitted means "whatever the model
    # does by default", which is the right answer for the eight jobs where
    # turning the chain of thought off has never been measured.
    #
    # Measured where it has been (2026-09-11, writing articles on ten identical
    # seeds): thinking off took the pass rate from 5/10 to 8/10 and the time
    # per piece from 61.6s to 9.1s, because 91% of the output tokens were going
    # to the chain of thought and the budget escalation was burning two wasted
    # calls per article. That is one task, not a general rule — 坑 §2.4.
    if thinking is not None:
        body["enable_thinking"] = thinking
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True
        # Ask for the token counts in the final chunk. Providers that do not
        # know this option ignore it, and the accounting simply comes out zero.
        body["stream_options"] = {"include_usage": True}
    return (
        f"{provider.base_url}/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body,
    )


def _anthropic_request(provider: Provider, key: str, messages: list[dict[str, str]],
                       *, max_tokens: int, temperature: float,
                       json_mode: bool, stream: bool = False,
                       thinking: bool | None = None) -> tuple[str, dict, dict]:
    # Anthropic takes the system prompt as a top-level field rather than as a
    # message, so it has to be lifted out of the list.
    system = " ".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": turns,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        body["system"] = system
    return (
        f"{provider.base_url}/v1/messages",
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
    )


def _read_stream(client: httpx.Client, url: str, headers: dict, body: dict
                 ) -> tuple[str, int, int, str]:
    """Read a server-sent-event completion, returning the same tuple as a parse.

    **Why streaming exists here at all: gateways time out on silence, not on
    length.** Writing a 450-word article takes gpt-5.5 about 109 seconds of
    thinking before its first visible token — and a relay that cuts idle
    connections at ~120s killed every attempt with HTTP 524 while the model was
    still working. Streaming keeps bytes moving, and the same request then
    finishes in 139 seconds. Measured 2026-09-09; without it, article generation
    simply could not complete.
    """
    text: list[str] = []
    tokens_in = tokens_out = 0
    finish = ""
    with client.stream("POST", url, headers=headers, json=body) as response:
        if response.status_code >= 400:
            response.read()
            detail = response.text[:400]
            raise LLMError(f"HTTP {response.status_code}: {detail}",
                           status=response.status_code,
                           retryable=response.status_code in RETRY_STATUS)
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:].strip()
            if chunk == "[DONE]":
                break
            try:
                event = json.loads(chunk)
            except ValueError:
                continue
            usage = event.get("usage") or {}
            if usage:
                tokens_in = int(usage.get("prompt_tokens", tokens_in) or tokens_in)
                tokens_out = int(usage.get("completion_tokens", tokens_out) or tokens_out)
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
    return "".join(text), tokens_in, tokens_out, finish


def _parse_openai(data: dict[str, Any]) -> tuple[str, int, int, str]:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("回复里没有 choices 字段")
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return (
        text,
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        str(choices[0].get("finish_reason") or ""),
    )


def _parse_anthropic(data: dict[str, Any]) -> tuple[str, int, int, str]:
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    usage = data.get("usage") or {}
    reason = str(data.get("stop_reason") or "")
    return (
        text,
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        "length" if reason == "max_tokens" else reason,
    )


def complete(
    provider: Provider,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    json_mode: bool = False,
    timeout: float | None = None,
    max_attempts: int | None = None,
    stream: bool | None = None,
    thinking: bool | None = None,
) -> Completion:
    """One completion, with retries. Raises :class:`LLMError` when it cannot.

    ``timeout`` and ``max_attempts`` default to the runtime settings, so a
    worker written months from now inherits whatever the user has tuned without
    having to thread the values through itself.
    """
    timeout = float(runtime_config.get("llm_timeout_seconds")) if timeout is None else timeout
    max_attempts = (
        int(runtime_config.get("llm_max_attempts")) if max_attempts is None else max_attempts
    )
    if stream is None:
        stream = bool(runtime_config.get("llm_stream"))
    # Not passed explicitly -> whatever the running job's setting says. The
    # framework applies it rather than each worker remembering to, because a
    # switch that every call site has to opt into is a switch that the tenth
    # call site will miss — the shape 坑 §5.1 cost four rounds to learn.
    if thinking is None:
        thinking = current_thinking()
    # Anthropic's event format is a different shape and nothing here needs it
    # yet; falling back keeps that branch honest rather than half-implemented.
    stream = stream and provider.kind == "openai"
    key = secrets_store.get_key(provider.id)
    if not key:
        raise LLMError(f"提供商 {provider.id} 还没有配置 API 密钥")

    build = _openai_request if provider.kind == "openai" else _anthropic_request

    last: LLMError | None = None
    budget = max_tokens
    for attempt in range(1, max_attempts + 1):
        url, headers, body = build(
            provider, key, messages,
            max_tokens=budget, temperature=temperature, json_mode=json_mode,
            stream=stream, thinking=thinking,
        )
        wait_hint: float | None = None
        try:
            _await_slot()
            if stream:
                with _client(timeout) as client:
                    streamed = _read_stream(client, url, headers, body)
                data = None
            else:
                with _client(timeout) as client:
                    response = client.post(url, headers=headers, json=body)

            if not stream and response.status_code >= 400:
                # The body often explains what is wrong; it is bounded here
                # because some gateways return an entire HTML error page.
                detail = response.text[:400]
                retryable = response.status_code in RETRY_STATUS
                wait_hint = _retry_after(response) if retryable else None
                raise LLMError(
                    f"HTTP {response.status_code}: {detail}",
                    status=response.status_code,
                    retryable=retryable,
                )

            if not stream:
                data = response.json()
        except httpx.HTTPError as exc:
            last = LLMError(f"网络错误：{exc}", retryable=True)
        except LLMError as exc:
            last = exc
        else:
            if stream:
                text, tokens_in, tokens_out, finish = streamed
            else:
                parse = _parse_openai if provider.kind == "openai" else _parse_anthropic
                text, tokens_in, tokens_out, finish = parse(data)

            # A reasoning model spends most of its output budget on the chain of
            # thought before writing anything visible. Ask a 27B thinking model
            # for 25 short glosses inside 1,500 tokens and the entire budget goes
            # to reasoning: finish_reason is "length" and the visible text is
            # empty — which then surfaces as an unhelpful "no JSON found".
            #
            # Rather than making the caller guess a number that depends on which
            # model is configured, double the budget and try again. It costs one
            # short call to discover, and it self-tunes for any model.
            if finish == "length" or not text.strip():
                if attempt < max_attempts and budget < MAX_OUTPUT_TOKENS:
                    budget = min(MAX_OUTPUT_TOKENS, budget * 3)
                    log.warning(
                        "llm.call.truncated",
                        f"{provider.id} 的回复被输出上限截断（思考型模型很费 token），"
                        f"把上限提到 {budget} 后重试",
                        provider=provider.id,
                        model=provider.model,
                        finish_reason=finish,
                        tokens_out=tokens_out,
                        new_budget=budget,
                    )
                    continue
                raise LLMError(
                    f"回复被输出上限截断（finish_reason={finish}，已用到 {budget} tokens）。"
                    "这个模型在思考上花的 token 很多——换一个非思考型模型，"
                    "或者把批量改小。",
                    retryable=False,
                )

            log.debug(
                "llm.call.ok",
                f"{provider.id} 返回 {tokens_out} 个输出 token",
                provider=provider.id,
                model=provider.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                attempt=attempt,
            )
            return Completion(
                text=text, tokens_in=tokens_in, tokens_out=tokens_out,
                model=provider.model, raw=data,
            )

        if not last.retryable or attempt == max_attempts:
            break
        # The server's own hint wins when it gave one: guessing shorter than it
        # asked simply spends another call to be refused again.
        delay = wait_hint if wait_hint else min(30.0, 2.0 ** attempt)
        # Jitter regardless: several job threads that hit the same limit must
        # not all come back at the same instant.
        delay += random.uniform(0, 1.0)  # noqa: S311
        log.warning(
            "llm.call.retry",
            f"{provider.id} 调用失败，{delay:.1f} 秒后重试（第 {attempt}/{max_attempts} 次）",
            provider=provider.id,
            status=last.status,
            attempt=attempt,
        )
        time.sleep(delay)

    assert last is not None  # noqa: S101 - the loop always sets it before breaking
    log.warning(
        "llm.call.failed",
        f"{provider.id} 调用最终失败：{last}",
        provider=provider.id,
        model=provider.model,
        status=last.status,
    )
    raise last
