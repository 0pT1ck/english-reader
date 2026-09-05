# Multi-architecture image: builds for both x86 and ARM64, so the same
# configuration runs on the development machine and on the NanoPi.
#
# Dependencies are installed in a separate layer from the application code, so
# editing code does not re-download packages or the language model.
FROM python:3.12-slim

# uv gives reproducible installs from the lockfile and is far faster than pip
# on the ARM box, where CPU is the scarce resource.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# Dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# The spaCy model is a separate download and belongs in the same cached layer
# as the dependencies — it changes far less often than application code.
RUN uv run python -m spacy download en_core_web_sm

# Application layer.
COPY backend/ ./backend/
COPY scripts/ ./scripts/

# Databases live on a mounted volume, never inside the image: rebuilding or
# upgrading the container must not touch study records.
VOLUME ["/app/data"]
ENV ER_DATA_DIR=/app/data \
    ER_HOST=0.0.0.0 \
    ER_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uv", "run", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
