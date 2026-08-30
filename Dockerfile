# Multi-stage: `dev` carries the test toolchain, `prod` does not.
#
# The split matters for image size. The host-only tooling (ruff, mypy, and friends)
# is several hundred MB and the runtime never imports it — baking the full dev
# extras into the shipped image inflates it several-fold for no benefit.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq for psycopg. Kept in `base` because the runtime needs it too.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# uv installs an order of magnitude faster than pip and resolves from the committed
# uv.lock, so the image gets the same versions as CI and the host venv.
RUN pip install --no-cache-dir uv

# Dependency layer first: copying only the manifests means a source edit does not
# invalidate the (slow) dependency install layer. `uv.lock*` is a glob so the build
# still works before the first lock is generated.
COPY pyproject.toml uv.lock* ./
# `--system` because a container needs no venv. NB: no `|| true` here — an earlier
# version swallowed install failures, which then resurfaced as an ImportError at
# runtime with nothing pointing back to the real cause.
RUN uv pip install --system --no-cache-dir -e .

FROM base AS dev
COPY pyproject.toml uv.lock* ./
RUN uv pip install --system --no-cache-dir -e ".[dev]"
COPY . .
CMD ["python", "-m", "social_scraper"]

FROM base AS prod
COPY . .
RUN uv pip install --system --no-cache-dir .
# Never run as root in the shipped image.
RUN useradd --create-home --uid 10001 appuser
USER appuser
CMD ["python", "-m", "social_scraper"]
