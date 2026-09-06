"""API keys, kept out of everything that leaves this machine.

``data/secrets.json`` is deliberately *not* one of the databases. Keys must not
be in ``learning.db`` or ``content.db``, because both have a download button
pointed at them — a backup mailed to somebody, or handed to whoever is helping
debug, would carry the credentials along. They are not in the diagnostic bundle
either, and the logging layer redacts anything that looks like one.

The file holds nothing but keys. Provider definitions (base_url, model, prices)
live in ``learning.db``, so restoring a backup brings the provider list back and
asks only for the keys again — which is the right split: the list is
configuration, the keys are secrets.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

from backend.core.config import get_settings
from backend.core.logging import get_logger

log = get_logger("llm.secrets")

_lock = threading.Lock()


def path() -> Path:
    return get_settings().data_dir / "secrets.json"


def _read() -> dict[str, Any]:
    target = path()
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt secrets file must not take the service down: every caller
        # already has to cope with "no key configured".
        log.exception("secrets.read.failed", "无法读取 secrets.json，视为没有配置密钥")
        return {}


def _write(data: dict[str, Any]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        # Owner-only. A no-op on Windows, which is where development happens,
        # but this file also ships to a Linux box where it does matter.
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_key(provider_id: str) -> str:
    with _lock:
        return str(_read().get("api_keys", {}).get(provider_id, ""))


def set_key(provider_id: str, api_key: str) -> None:
    with _lock:
        data = _read()
        data.setdefault("api_keys", {})[provider_id] = api_key
        _write(data)
    # The key itself never reaches the log — only the fact that one was stored.
    log.info("secrets.key.stored", f"已保存 {provider_id} 的 API 密钥", provider=provider_id)


def delete_key(provider_id: str) -> bool:
    with _lock:
        data = _read()
        removed = data.get("api_keys", {}).pop(provider_id, None) is not None
        if removed:
            _write(data)
    if removed:
        log.info("secrets.key.removed", f"已删除 {provider_id} 的 API 密钥", provider=provider_id)
    return removed


def configured_providers() -> set[str]:
    """Which providers have a key, without revealing any of them."""
    with _lock:
        return {k for k, v in _read().get("api_keys", {}).items() if v}
