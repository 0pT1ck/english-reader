"""Two separate authentication schemes, deliberately kept apart.

Architecture rule 4: ``/v1/client/…`` and ``/v1/admin/…`` are different surfaces
with different powers. A device token can read study content and report events;
it can never reach an admin endpoint. The admin surface can download the entire
learning database, rewrite settings and force regeneration — so it gets its own
credential and never shares one with the clients.

Why an admin password at all, on a device sitting on a home LAN: because the
plan is to move this to a cloud server later, and retrofitting authentication
onto endpoints written without it is exactly the kind of change that gets half
done. Adding it now costs one environment variable.

Device tokens are per-device so a lost phone can be revoked on its own without
disturbing anything else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from fastapi import Cookie, Header, Request

from backend.core.config import get_settings
from backend.core.db import Migration, get_connection
from backend.core.errors import Forbidden, Unauthorized
from backend.core.logging import get_logger

log = get_logger("core.auth")

SESSION_COOKIE = "er_admin"
SESSION_TTL_SECONDS = 30 * 24 * 3600

MIGRATIONS = [
    Migration(
        version=1,
        name="device tokens",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS devices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            token_hash   TEXT    NOT NULL UNIQUE,
            created_at   TEXT    NOT NULL,
            last_seen_at TEXT,
            revoked_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_devices_hash ON devices (token_hash);
        """,
    ),
    Migration(
        version=2,
        name="device belongs to a learner",
        database="learning",
        # The identity hook. There is exactly one learner and this column is
        # always 1 — no registration, no login, nothing reads it as a variable
        # yet. It exists because P2 is the moment the client contract is
        # written, and architecture rule 5 forbids removing or repurposing a
        # field afterwards: a contract that hardcodes "there is only one person"
        # leaves /v2/ as the only way to ever add a second one.
        #
        # Identity is derived from the device token, never self-reported by the
        # client. Adding real users later means giving this column real values
        # and nothing else — no sideloaded client needs updating.
        apply="ALTER TABLE devices ADD COLUMN learner_id INTEGER NOT NULL DEFAULT 1;",
    ),
]

#: The single learner, until there is a reason for more. Named rather than
#: written as a bare 1 so every place that assumes it is greppable.
SOLE_LEARNER_ID = 1


# --------------------------------------------------------------------------- #
# Admin sessions
# --------------------------------------------------------------------------- #


def _secret_bytes() -> bytes:
    return get_settings().admin_secret.encode("utf-8")


def _sign(payload: str) -> str:
    digest = hmac.new(_secret_bytes(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_session() -> str:
    """Mint a signed session token.

    Self-contained and signed with the admin secret rather than stored in a
    table: there is exactly one admin, sessions carry no state worth persisting,
    and rotating the secret invalidates every session for free.
    """
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"admin.{expires}"
    return f"{payload}.{_sign(payload)}"


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        subject, expires_raw, signature = token.rsplit(".", 2)
        payload = f"{subject}.{expires_raw}"
    except ValueError:
        return False

    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        return int(expires_raw) > int(time.time())
    except ValueError:
        return False


def check_admin_secret(candidate: str) -> bool:
    """Constant-time comparison, so a wrong password cannot be found by timing."""
    return hmac.compare_digest(candidate.encode("utf-8"), _secret_bytes())


async def require_admin(
    er_admin: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> None:
    """Dependency guarding every admin endpoint.

    Accepts either the session cookie (browser) or the raw secret in a header
    (command line, and the AI querying diagnostics directly).
    """
    if verify_session(er_admin):
        return
    if x_admin_secret and check_admin_secret(x_admin_secret):
        return
    raise Unauthorized("需要管理员认证")


def is_admin_request(request: Request) -> bool:
    """Non-raising variant, for server-rendered pages that redirect to login."""
    if verify_session(request.cookies.get(SESSION_COOKIE)):
        return True
    header = request.headers.get("X-Admin-Secret")
    return bool(header and check_admin_secret(header))


# --------------------------------------------------------------------------- #
# Device tokens
# --------------------------------------------------------------------------- #


def _hash_token(token: str) -> str:
    """Store only the hash.

    A leaked backup of learning.db should not hand over working credentials for
    every device.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_device(name: str) -> str:
    """Register a device and return its token. The token is shown exactly once."""
    token = secrets.token_urlsafe(32)
    conn = get_connection("learning")
    conn.execute(
        "INSERT INTO devices (name, token_hash, created_at) VALUES (?, ?, ?)",
        (name, _hash_token(token), datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    log.info("device.created", f"注册了新设备：{name}", device=name)
    return token


def revoke_device(device_id: int) -> bool:
    conn = get_connection("learning")
    cursor = conn.execute(
        "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), device_id),
    )
    conn.commit()
    if cursor.rowcount:
        log.info("device.revoked", "设备令牌已吊销", device_id=device_id)
    return bool(cursor.rowcount)


def list_devices() -> list[dict]:
    rows = get_connection("learning").execute(
        "SELECT id, name, learner_id, created_at, last_seen_at, revoked_at"
        " FROM devices ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def learner_for_device(device_id: int) -> int:
    """Which learner this device belongs to.

    Always returns 1 today. Client endpoints call this instead of writing the
    constant inline, so switching to real multi-user work is a change to this
    function and the column behind it — not a sweep through every endpoint.
    """
    row = get_connection("learning").execute(
        "SELECT learner_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    return int(row["learner_id"]) if row else SOLE_LEARNER_ID


def learner_profile(learner_id: int) -> dict:
    """The ``learner`` block echoed in every client response.

    Lets a client notice it is looking at someone else's cache and drop it.
    ``level`` is the reserved slot for P3's ability estimate — declared now,
    null until then, with ``capabilities.level_estimate`` saying which it is.
    """
    return {"id": learner_id, "name": "本人", "level": None}


async def require_device(
    authorization: str | None = Header(default=None),
) -> int:
    """Dependency guarding every client endpoint. Returns the device id.

    Deliberately does *not* accept the admin secret: the client surface must not
    become a way to reach admin capability, and an admin credential travelling
    to a phone would defeat the separation.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("缺少设备令牌")

    token = authorization.split(" ", 1)[1].strip()
    row = get_connection("learning").execute(
        "SELECT id, revoked_at FROM devices WHERE token_hash = ?",
        (_hash_token(token),),
    ).fetchone()

    if row is None:
        raise Unauthorized("设备令牌无效")
    if row["revoked_at"]:
        raise Forbidden("该设备令牌已被吊销")

    conn = get_connection("learning")
    conn.execute(
        "UPDATE devices SET last_seen_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]),
    )
    conn.commit()
    return int(row["id"])
