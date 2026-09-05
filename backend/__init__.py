"""English Reader backend.

A headless service with replaceable clients (architecture rule 1): all study
logic lives here; clients render, collect interactions and report events.
"""

import sys

# Windows terminals default to a legacy code page, which turns every Chinese
# message into mojibake. Done here — the earliest point any entry point reaches,
# whether that is the launcher, a script, or uvicorn — so that anything printed
# afterwards is readable. The user watches this terminal; it has to work out of
# the box rather than depend on an environment variable being set.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - non-standard streams
        pass
