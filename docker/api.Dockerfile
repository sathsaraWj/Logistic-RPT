# Container image for apps/api (Phase 14 addendum: Cloud Run deployment).
#
# Includes the `ml` dependency group (torch/mlflow/scikit-learn) even though most API requests
# never touch it: `hermes_rpt.inference.model_loading` imports `mlflow` unconditionally at
# module load time (via apps/api/routers/predictions.py), so the process cannot start without
# it installed — this is not optional for this image, see docs/DEPLOYMENT.md.
#
# Multi-stage: the builder stage resolves the locked dependency set with `uv` into a venv; the
# runtime stage copies only that venv + the application source, never the build toolchain, and
# runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first (better layer caching — this layer only invalidates when the lock file
# changes, not on every source edit).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group ml --no-install-project

# Now the actual source, then install the project itself into the same venv.
COPY src ./src
COPY apps ./apps
COPY README.md ./
RUN uv sync --frozen --no-dev --group ml

FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system hermes && useradd --system --gid hermes --create-home hermes

WORKDIR /app
COPY --from=builder --chown=hermes:hermes /app/.venv /app/.venv
COPY --from=builder --chown=hermes:hermes /app/src /app/src
COPY --from=builder --chown=hermes:hermes /app/apps /app/apps

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER hermes

# Cloud Run injects $PORT (defaults to 8080) and expects the container to bind to it — never
# hardcode 8000 here even though that's this project's local-dev convention (README.md).
EXPOSE 8080
CMD ["sh", "-c", "uvicorn apps.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
