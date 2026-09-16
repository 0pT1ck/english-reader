# syntax=docker/dockerfile:1
#
# Two stages. The builder holds uv, the compiler toolchain and the wheel cache;
# the runtime holds only the interpreter, the virtualenv and the application.
# The split is not about elegance — it is about what ends up exposed on a public
# server, and a build toolchain is not something a deployed image should carry.
#
# Build:   docker build -f docker/Dockerfile -t english-reader .
# Deploy:  see docker/docker-compose.yml

ARG PYTHON_VERSION=3.12-slim

# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION} AS builder

# Where Python packages come from. Defaults to PyPI; override it where PyPI is
# far away. On the Chinese host this build targets, the difference measured
# 56 KB/s against 21 MB/s, which is the difference between a build and an
# afternoon. `uv sync --frozen` still honours the lockfile — the lock pins
# versions and hashes, and a mirror serves the same artefacts.
ARG PYPI_INDEX=https://pypi.org/simple

# uv from PyPI rather than `COPY --from=ghcr.io/astral-sh/uv`, so the build
# needs exactly one package host instead of two. Pinned: `latest` would make
# the build unreproducible and invalidate every layer below it whenever
# upstream publishes.
RUN pip install --no-cache-dir --index-url ${PYPI_INDEX} uv==0.12.13

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_DEFAULT_INDEX=${PYPI_INDEX}

# Dependency layer, kept separate from application code so editing a .py file
# does not re-download PyTorch-sized wheels.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The spaCy model is a ~12 MB download from GitHub releases, which is the single
# most likely step to fail on a mainland Chinese host — so it happens here, on
# whatever machine has working egress, and never at runtime on the server.
#
# Installed by URL rather than `spacy download`, because that command resolves a
# version at build time (unpinned, so two builds can differ) and needs pip inside
# the environment.
ARG SPACY_MODEL_URL=https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
RUN uv pip install --python /app/.venv "${SPACY_MODEL_URL}"

# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION} AS runtime

# Runs as a normal user. On a home LAN this was pedantry; on a cloud server with
# a public port it is the difference between a bug in a request handler and a
# bug in a request handler that runs as root.
#
# The uid is a build argument because `data/` is bind-mounted from the host and
# the kernel checks uid numbers, not names: a container user the host directory
# does not grant write access to fails at startup with a bare PermissionError.
# Either build with the host owner's uid, or override it per-run — compose does
# the latter through ER_UID/ER_GID.
ARG APP_UID=10001
RUN useradd --create-home --uid ${APP_UID} reader

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

# Writable by whatever uid the container is given. Without it, running under an
# overridden uid that has no /etc/passwd entry leaves HOME unset, and libraries
# that want a cache directory fail in obscure ways.
ENV HOME=/tmp

COPY --from=builder --chown=reader:reader /app/.venv /app/.venv

# Application layer. Only what the server actually runs: no client/, no docs/,
# no tests — see .dockerignore for what never even reaches the daemon.
COPY --chown=reader:reader backend/ ./backend/
COPY --chown=reader:reader scripts/ ./scripts/

# Databases live on a mounted volume, never inside the image: rebuilding or
# upgrading the container must not touch study records.
#
# Creating it here only helps the named-volume case, where Docker seeds a fresh
# volume from the image and carries this ownership across. A bind mount — which
# is what the compose file uses — keeps the host directory's own owner, so that
# one is handled with ER_UID/ER_GID instead.
RUN mkdir -p /app/data && chown reader:reader /app/data

ENV ER_DATA_DIR=/app/data \
    ER_HOST=0.0.0.0 \
    ER_PORT=8000

# Containers default to UTC, and this application reads the *local* clock:
# core/tasks.py schedules "04:00" as 4am where the machine is, and the review
# day boundary is local midnight. Left at UTC on a Chinese server, the nightly
# generation job runs at noon and "today" flips eight hours early — with no
# error anywhere. The base image already carries tzdata, so this is one line.
ENV TZ=Asia/Shanghai

USER reader
EXPOSE 8000

# start-period is generous because the first start runs schema migrations across
# four SQLite files; a container killed halfway through that is how you get a
# half-migrated database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# uvicorn directly, not run.py: that launcher exists to open a browser and print
# Chinese instructions for someone double-clicking a .bat file, none of which
# applies here.
# --proxy-headers so the app sees the real client address and scheme rather
# than Caddy's; --forwarded-allow-ips=* is safe only because nothing but Caddy
# can reach this port — the compose file publishes no port for this service.
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
