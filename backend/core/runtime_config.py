"""Runtime configuration: settings that can be changed without a restart.

Startup config (:mod:`backend.core.config`) covers paths, secrets and the bind
address — things a restart is the natural way to change. Everything else lives
here: stored in ``learning.db``, edited from the admin console, effective
immediately.

**Specs are declared by whoever owns the setting.** Core declares log retention
and Bark; each feature module declares its own knobs when it arrives. Nothing
maintains a central list of every setting in the system, which is what stops
this file from becoming the place every phase has to edit.

Deliberately *not* declared here yet: study parameters like target unknown-word
rate or the daily new-target-word cap. Those belong to the phases that
implement the behaviour they control — declaring them now would be building
the learning phases during P0.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Literal

from backend.core.db import Migration, get_connection
from backend.core.logging import get_logger

log = get_logger("core.config")

ValueType = Literal["str", "int", "float", "bool", "json"]

MIGRATIONS = [
    Migration(
        version=1,
        name="runtime settings table",
        database="ops",
        apply="""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]


@dataclass(frozen=True)
class ConfigSpec:
    """Describes one runtime setting, including how to render it for editing.

    ``title`` and ``description`` are Chinese because they appear in the admin
    console, which the user reads. The ``key`` stays an English identifier.
    """

    key: str
    default: Any
    value_type: ValueType
    title: str
    description: str = ""
    group: str = "general"
    order: int = 100

    #: Whether the value is a credential. Marking it here — at the point the
    #: setting is declared, by whoever owns it — is what keeps the diagnostic
    #: bundle safe as settings are added: the exporter drops everything flagged,
    #: instead of maintaining a blacklist that a new phase can forget to update.
    secret: bool = False


_specs: dict[str, ConfigSpec] = {}
_cache: dict[str, Any] = {}
_lock = threading.Lock()


def register(*specs: ConfigSpec) -> None:
    """Declare settings. Called at import time by whoever owns them."""
    for spec in specs:
        if spec.key in _specs and _specs[spec.key] != spec:
            log.warning(
                "config.spec.conflict",
                f"配置项 {spec.key} 被重复声明且定义不同，保留先注册的那个",
                key=spec.key,
            )
            continue
        _specs[spec.key] = spec


def specs() -> list[ConfigSpec]:
    """All declared settings, grouped and ordered for the admin console."""
    return sorted(_specs.values(), key=lambda s: (s.group, s.order, s.key))


def _coerce(raw: str, value_type: ValueType) -> Any:
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "json":
        return json.loads(raw)
    return raw


def _encode(value: Any, value_type: ValueType) -> str:
    if value_type == "json":
        return json.dumps(value, ensure_ascii=False)
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


def get(key: str) -> Any:
    """Read a setting, falling back to its declared default.

    Cached in memory: settings are read on nearly every request path and change
    perhaps a handful of times in the project's life.
    """
    if key in _cache:
        return _cache[key]

    spec = _specs.get(key)
    if spec is None:
        raise KeyError(f"未声明的配置项: {key}")

    with _lock:
        try:
            row = get_connection("ops").execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        except Exception:  # noqa: BLE001 - before migrations have run
            row = None

        if row is None:
            value = spec.default
        else:
            try:
                value = _coerce(row["value"], spec.value_type)
            except (ValueError, json.JSONDecodeError):
                log.warning(
                    "config.value.invalid",
                    f"配置项 {key} 的存储值无法解析，回退到默认值",
                    key=key,
                    stored=row["value"],
                )
                value = spec.default

        _cache[key] = value
        return value


def set(key: str, value: Any) -> None:  # noqa: A001 - reads naturally as config.set
    """Write a setting and invalidate the cache."""
    spec = _specs.get(key)
    if spec is None:
        raise KeyError(f"未声明的配置项: {key}")

    encoded = _encode(value, spec.value_type)
    # Validate by round-tripping, so a bad value is rejected before it is stored.
    parsed = _coerce(encoded, spec.value_type)

    with _lock:
        conn = get_connection("ops")
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, encoded),
        )
        conn.commit()
        _cache[key] = parsed

    log.info("config.updated", f"配置项 {key} 已更新", key=key)


def all_values(*, include_secrets: bool = True) -> dict[str, Any]:
    """Current value of every declared setting.

    ``include_secrets=False`` drops everything a spec marked as a credential —
    used by the diagnostic bundle, which is a file the user mails to somebody
    else.
    """
    return {
        spec.key: get(spec.key)
        for spec in _specs.values()
        if include_secrets or not spec.secret
    }


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# Settings owned by core
# --------------------------------------------------------------------------- #

register(
    ConfigSpec(
        key="log_retention_days",
        default=30,
        value_type="int",
        title="技术日志保留天数",
        description="超过这个天数的技术日志会被自动清理。决策日志不受影响，永久保留。",
        group="diagnostics",
        order=10,
    ),
    ConfigSpec(
        key="bark_enabled",
        default=False,
        value_type="bool",
        title="启用 Bark 通知",
        description="出现生成失败、服务异常或严重错误时推送到手机。",
        group="diagnostics",
        order=20,
    ),
    ConfigSpec(
        key="bark_url",
        default="",
        value_type="str",
        title="Bark 推送地址",
        description="形如 https://api.day.app/你的密钥 。官方服务器在境外，"
        "内网部署时需要配置代理；自建 Bark 服务端则填内网地址。",
        group="diagnostics",
        order=21,
        secret=True,  # the path component is the push credential
    ),
    ConfigSpec(
        key="min_contract_version",
        default=0,
        value_type="int",
        title="客户端契约版本下限",
        description="低于这个版本的客户端一律拒绝（HTTP 426），只拦 /v1/client/ 那一半。"
        "0 ＝ 不拦。**发版顺序是先发客户端、再把这个数调上去**——反过来就是把自己"
        "锁在外面，而唯一能改这个设置的开发者选项也在手机上。"
        "客户端当前发的版本号见 ERCore/Transport.swift 的 ContractVersion.current。",
        group="api",
        order=10,
    ),
    ConfigSpec(
        key="alert_dedupe_minutes",
        default=30,
        value_type="int",
        title="同类告警去重间隔（分钟）",
        description="同一类错误在这个时间窗内只推送一次，避免一个故障刷屏。",
        group="diagnostics",
        order=22,
    ),
)
