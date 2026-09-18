"""Startup configuration, read from environment variables and `.env`.

This project keeps two kinds of configuration deliberately apart:

* **Startup config** (this module) — filesystem paths, secrets, bind address,
  outbound proxy. Changing any of these requires a restart anyway, so they live
  in the environment where a restart is the expected way to apply a change.
* **Runtime config** (:mod:`backend.core.runtime_config`) — study parameters,
  Bark URL, log retention. These live in ``learning.db`` and are editable from
  the admin console without touching the machine.

Architecture rule 6 requires parameters to be configurable rather than
hardcoded. This split is how that rule is honoured without turning every
trivial setting into a database round-trip on the hot path.

Terminology note for future maintainers: this codebase uses English identifiers
throughout, but the design documents (``docs/``) are written in Chinese. Key
term mappings appear in the module that owns each concept.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: backend/core/config.py -> backend/core -> backend -> <root>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Values fixed at process start.

    Every field can be overridden with an ``ER_``-prefixed environment variable,
    e.g. ``ER_PORT=9000``. Values in ``.env`` are read too, which is how the
    admin secret survives restarts on a machine where nobody wants to manage
    environment variables by hand.
    """

    model_config = SettingsConfigDict(
        env_prefix="ER_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -------------------------------------------------------------

    data_dir: Path = Field(
        default=PROJECT_ROOT / "data",
        description="Directory holding the SQLite files. Mounted from the "
        "host when running in Docker so container rebuilds never touch "
        "study records.",
    )

    # --- security ------------------------------------------------------------

    admin_secret: str = Field(
        default="",
        description="Password for the admin console. Empty means 'not configured "
        "yet' — the app generates one on first start and writes it to .env "
        "rather than shipping a default, because a default admin password on "
        "something that will later be exposed to the internet is a trap.",
    )

    # --- network -------------------------------------------------------------

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)

    proxy_url: str | None = Field(
        default=None,
        description="Outbound proxy for LLM APIs, dictionary downloads and Bark "
        "push. On the development machine this is the local v2rayN mixed port; "
        "on a cloud server it is usually unset. Never hardcode it — see "
        "CLAUDE.md, network access.",
    )

    # --- diagnostics ---------------------------------------------------------

    log_level: str = Field(
        default="INFO",
        description="Floor for logs written to disk. DEBUG records are always "
        "captured in memory regardless, and flushed to disk when an ERROR "
        "occurs in the same trace — see core.logging.",
    )

    dev_mode: bool = Field(
        default=False,
        description="Enables auto-reload and more verbose error pages. Never "
        "enable on a deployed instance.",
    )

    # --- derived paths -------------------------------------------------------
    #
    # Five separate SQLite files, not one. They are split by the only question
    # that matters operationally — *what happens if this file is lost?*
    #
    #   dictionary.db  re-import it            (hundreds of MB, rebuildable)
    #   logs.db        shrug                   (disposable by design)
    #   ops.db         re-pair and re-configure (annoying, not a loss)
    #   content.db     pay for it again        (LLM-generated, must be backed up)
    #   events.db      unrecoverable           (must be backed up)
    #
    # Keeping the three cheap ones out of the backup is what makes a backup a
    # small file that can be mailed around rather than a multi-hundred-MB dump.
    #
    # **2026-09-18 (P9 §10): `learning.db` was split into the last three.**
    # It had grown into "everything that is not a dictionary" — articles,
    # tokens, device tokens, settings, the task schedule, the LLM job log and
    # the learning record, all in one file, all answering that question
    # differently. The backup rule could therefore only be honest about the
    # whole file, and the whole file is 43 MB of which the part that is really
    # unrecoverable is under one. See `docs/phase-9.html` §10.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dictionary_db(self) -> Path:
        """Read-only reference data imported from ECDICT.

        Can be deleted and rebuilt from the import script at any time — which is
        exactly why nothing generated by an LLM may be stored here.
        """
        return self.data_dir / "dictionary.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_db(self) -> Path:
        """Generated reference content: senses, examples, word families, affixes.

        Separate from dictionary.db because it cost money to produce and cannot
        be rebuilt from a download; separate from learning.db because it is not
        the user's own record — it is the same for every learner. Backed up
        alongside learning.db.
        """
        return self.data_dir / "content.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def events_db(self) -> Path:
        """The learning record, as the server holds it.

        **2026-09-18 (P9 §10): this is the half of ``learning.db`` worth keeping.**
        The device is the first copy of the learning record now; this file is
        where several devices meet and what survives losing one. It holds the
        uploaded events, the pool snapshot, the decision log, and the derived
        tables the server keeps from replaying those events.

        Backed up, and the admin console's backup button downloads it.
        """
        return self.data_dir / "events.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ops_db(self) -> Path:
        """How this installation is set up and what it has been doing.

        Device tokens, runtime settings, the task schedule, the LLM job log.
        **Not backed up on purpose**: losing it costs a re-pairing and a few
        settings, and keeping it out is what makes a backup small enough to mail.
        """
        return self.data_dir / "ops.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def logs_db(self) -> Path:
        """Technical logs. Rotated and disposable.

        Decision logs do *not* live here — they go to events.db, because
        'why did the system pick this article three months ago' has long-term
        value while 'which HTTP requests happened' does not.
        """
        return self.data_dir / "logs.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def legacy_learning_db(self) -> Path:
        """The file the three above were carved out of (P9 §10).

        Only :mod:`backend.core.resplit` reads it, and only to empty it. It is
        not deleted once emptied: an empty file next to the new three is a
        readable "this already happened", and deleting a database nobody asked
        to delete is not a thing this project does on its own.
        """
        return self.data_dir / "learning.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def backup_dir(self) -> Path:
        """Automatic pre-migration backups of learning.db land here."""
        return self.data_dir / "backups"


def _ensure_admin_secret(settings: Settings) -> tuple[Settings, str | None]:
    """Generate and persist an admin secret on first run.

    Returns the settings and, when a secret was just created, its plaintext so
    the caller can show it once at startup.

    Rationale: the user of this project does not read code, so failing to boot
    with "ER_ADMIN_SECRET is required" would be a dead end. Generating a strong
    random secret and writing it to ``.env`` keeps the deployment safe by
    default while still being a single readable line the user can look up.
    """
    if settings.admin_secret:
        return settings, None

    generated = secrets.token_urlsafe(32)

    # Checked before opening: open("a") would create the file, making the size
    # check meaningless on the very first run.
    needs_separator = ENV_FILE.exists() and ENV_FILE.stat().st_size > 0

    # Append rather than rewrite: .env may hold other local overrides.
    with ENV_FILE.open("a", encoding="utf-8") as fh:
        if needs_separator:
            fh.write("\n")
        fh.write("# Generated automatically on first start. Keep this private.\n")
        fh.write(f"ER_ADMIN_SECRET={generated}\n")

    settings = settings.model_copy(update={"admin_secret": generated})
    return settings, generated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, creating directories on first call.

    Cached because these values cannot change without a restart; anything that
    *should* be changeable at runtime belongs in
    :mod:`backend.core.runtime_config` instead.
    """
    settings = Settings()
    settings, _ = _ensure_admin_secret(settings)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    return settings
