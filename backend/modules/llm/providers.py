"""Provider records: what to call, with what model, at what price.

Two wire formats are supported, and that is enough to reach nearly everything:

* ``openai`` — the de-facto standard. DeepSeek, Kimi, Qwen, Zhipu, Doubao and
  every relay service expose it, so one implementation covers them all;
  configuring a new one is three fields.
* ``anthropic`` — a different request and response shape, worth its own branch.

Nothing here knows an API key. Keys live in :mod:`.secrets_store` and are joined
in only at call time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.core.db import get_connection
from backend.core.errors import InvalidRequest, NotFound
from backend.modules.llm import secrets_store

KINDS = {"openai": "OpenAI 兼容", "anthropic": "Anthropic"}

# Sensible starting points, offered in the console as one-click presets. Prices
# are per million tokens and go stale — they are a starting value the user
# edits, not a fact the code relies on.
PRESETS = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "price_in": 2.0,
        "price_out": 8.0,
        "currency": "CNY",
    },
    {
        "id": "kimi",
        "label": "Kimi (Moonshot)",
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "price_in": 12.0,
        "price_out": 12.0,
        "currency": "CNY",
    },
    {
        "id": "qwen",
        "label": "通义千问",
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "price_in": 0.8,
        "price_out": 2.0,
        "currency": "CNY",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "price_in": 5.0,
        "price_out": 5.0,
        "currency": "CNY",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "price_in": 1.1,
        "price_out": 4.4,
        "currency": "USD",
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5",
        "price_in": 3.0,
        "price_out": 15.0,
        "currency": "USD",
    },
]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str
    base_url: str
    model: str
    price_in: float
    price_out: float
    currency: str
    enabled: bool
    is_default: bool
    note: str = ""

    @property
    def has_key(self) -> bool:
        return bool(secrets_store.get_key(self.id))

    def cost_of(self, tokens_in: int, tokens_out: int) -> float:
        """Money for one call, in this provider's currency."""
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def as_dict(self, *, with_key_state: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "kind_label": KINDS.get(self.kind, self.kind),
            "base_url": self.base_url,
            "model": self.model,
            "price_in": self.price_in,
            "price_out": self.price_out,
            "currency": self.currency,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "note": self.note,
        }
        if with_key_state:
            # Whether a key exists — never the key.
            data["has_key"] = self.has_key
        return data


def _from_row(row: Any) -> Provider:
    return Provider(
        id=row["id"],
        label=row["label"],
        kind=row["kind"],
        base_url=row["base_url"].rstrip("/"),
        model=row["model"],
        price_in=row["price_in"],
        price_out=row["price_out"],
        currency=row["currency"],
        enabled=bool(row["enabled"]),
        is_default=bool(row["is_default"]),
        note=row["note"] or "",
    )


def all_providers() -> list[Provider]:
    rows = get_connection("ops").execute(
        "SELECT * FROM llm_providers ORDER BY is_default DESC, label"
    ).fetchall()
    return [_from_row(row) for row in rows]


def get(provider_id: str) -> Provider:
    row = get_connection("ops").execute(
        "SELECT * FROM llm_providers WHERE id = ?", (provider_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"没有配置名为 {provider_id} 的提供商")
    return _from_row(row)


def default_provider() -> Provider:
    """The provider a job uses when none was named.

    Falls back to any enabled provider that has a key, so a single configured
    provider works without the user ever having to mark it default.
    """
    candidates = [p for p in all_providers() if p.enabled and p.has_key]
    if not candidates:
        raise InvalidRequest("还没有可用的提供商——请先添加一个并填入 API 密钥")
    for provider in candidates:
        if provider.is_default:
            return provider
    return candidates[0]


def upsert(**fields: Any) -> Provider:
    """Create or update a provider. Only the id is immutable."""
    provider_id = str(fields.get("id", "")).strip().lower()
    if not _SLUG.match(provider_id):
        raise InvalidRequest("提供商 id 只能用小写字母、数字、连字符和下划线", id=provider_id)
    kind = str(fields.get("kind", "openai"))
    if kind not in KINDS:
        raise InvalidRequest("未知的接口类型", kind=kind)

    base_url = str(fields.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise InvalidRequest("base_url 必须以 http:// 或 https:// 开头")

    model = str(fields.get("model", "")).strip()
    if not model:
        raise InvalidRequest("必须指定模型名")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_connection("ops")
    conn.execute(
        "INSERT INTO llm_providers (id, label, kind, base_url, model, price_in,"
        " price_out, currency, enabled, is_default, note, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET label=excluded.label, kind=excluded.kind,"
        " base_url=excluded.base_url, model=excluded.model, price_in=excluded.price_in,"
        " price_out=excluded.price_out, currency=excluded.currency,"
        " enabled=excluded.enabled, is_default=excluded.is_default,"
        " note=excluded.note, updated_at=excluded.updated_at",
        (
            provider_id,
            str(fields.get("label") or provider_id),
            kind,
            base_url,
            model,
            float(fields.get("price_in") or 0),
            float(fields.get("price_out") or 0),
            str(fields.get("currency") or "CNY"),
            1 if fields.get("enabled", True) else 0,
            1 if fields.get("is_default") else 0,
            str(fields.get("note") or ""),
            now,
            now,
        ),
    )
    if fields.get("is_default"):
        # Exactly one default: setting a new one clears the old.
        conn.execute("UPDATE llm_providers SET is_default = 0 WHERE id != ?", (provider_id,))
    conn.commit()
    return get(provider_id)


def delete(provider_id: str) -> bool:
    conn = get_connection("ops")
    cursor = conn.execute("DELETE FROM llm_providers WHERE id = ?", (provider_id,))
    conn.commit()
    secrets_store.delete_key(provider_id)
    return bool(cursor.rowcount)
