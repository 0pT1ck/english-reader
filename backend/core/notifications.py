"""Push notifications via Bark.

Purpose: tell the user something broke *before* they discover it by opening the
app and finding no content. The canonical case is the nightly generation task
failing at 4am — without a push, the first sign is an empty morning.

Design constraint that matters more than it looks: **an alerting channel that
cries wolf gets ignored.** So only ERROR and CRITICAL fire, and identical
failures are deduplicated within a configurable window. A single fault must
produce a single notification, not one per retry.

Sending happens on a background thread. A push that hangs — plausible, since
the official Bark server is overseas and may need the proxy — must never delay
the request that triggered it.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from backend.core import runtime_config
from backend.core.config import get_settings
from backend.core.logging import add_alert_handler, get_logger

log = get_logger("core.notifications")

# event name -> last push timestamp. Bounded implicitly: distinct error events
# in this application are few, and stale entries are harmless.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _proxy() -> str | None:
    """Outbound proxy, if configured.

    Bark's official server is reachable only through the proxy on the
    development machine and on the home network device; a self-hosted Bark on
    the LAN needs no proxy at all. Both work by leaving this to configuration.
    """
    return get_settings().proxy_url


def send(title: str, body: str, *, group: str = "EnglishReader", url: str = "") -> bool:
    """Send one push. Returns whether it was accepted.

    Failures are logged at WARNING, never ERROR — escalating a failed alert into
    another alert is how notification loops start.
    """
    if not runtime_config.get("bark_enabled"):
        return False

    base = str(runtime_config.get("bark_url")).rstrip("/")
    if not base:
        log.warning("bark.not_configured", "已启用 Bark 但未填写推送地址")
        return False

    endpoint = f"{base}/{quote(title, safe='')}/{quote(body, safe='')}"
    params: dict[str, str] = {"group": group}
    if url:
        # Tapping the notification opens the relevant admin page directly.
        params["url"] = url

    try:
        proxy = _proxy()
        with httpx.Client(timeout=10.0, proxy=proxy) as client:
            response = client.get(endpoint, params=params)
        if response.status_code >= 400:
            log.warning(
                "bark.rejected",
                f"Bark 推送被拒绝，HTTP {response.status_code}",
                status=response.status_code,
            )
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("bark.failed", f"Bark 推送失败：{exc}", error=str(exc))
        return False


def send_async(title: str, body: str, *, group: str = "EnglishReader", url: str = "") -> None:
    """Fire and forget, off the calling thread."""
    thread = threading.Thread(
        target=send,
        args=(title, body),
        kwargs={"group": group, "url": url},
        daemon=True,
        name="bark-push",
    )
    thread.start()


def _should_send(event: str) -> bool:
    """Deduplicate by event name within the configured window."""
    window_seconds = max(0, int(runtime_config.get("alert_dedupe_minutes"))) * 60
    now = time.monotonic()
    with _lock:
        previous = _last_sent.get(event)
        if previous is not None and (now - previous) < window_seconds:
            return False
        _last_sent[event] = now
        return True


def _alert_handler(record: dict[str, Any]) -> None:
    """Bridge from the logging subsystem to push notifications.

    Registered rather than imported by :mod:`backend.core.logging`, which keeps
    logging free of any dependency on configuration or HTTP.
    """
    event = record.get("event", "unknown")
    if not _should_send(event):
        return

    level = record.get("level", "ERROR")
    trace_id = record.get("trace_id") or "-"
    title = f"English Reader · {level}"
    body = f"{record.get('message', '')}\n{event} · trace {trace_id}"
    send_async(title, body)


def install() -> None:
    """Wire notifications into the logging subsystem. Called once at startup."""
    add_alert_handler(_alert_handler)


def test_push() -> bool:
    """Send a test notification. Backs the admin console's test button."""
    return send(
        "English Reader",
        "测试通知：如果你看到这条消息，说明 Bark 推送配置正确。",
    )
