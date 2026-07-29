"""Hermes-RPT public/internal HTTP API entry point.

Run locally with: `uv run uvicorn apps.api.main:app --reload`
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from apps.api.exception_handlers import register_exception_handlers
from apps.api.routers import (
    auth,
    connections,
    features,
    mappings,
    memberships,
    monitoring,
    predictions,
    schema_discovery,
)
from hermes_rpt import __version__
from hermes_rpt.common.correlation import CorrelationIdMiddleware
from hermes_rpt.common.db import get_sessionmaker
from hermes_rpt.common.logging import configure_logging, get_logger
from hermes_rpt.common.settings import get_settings

_START_TIME = time.monotonic()


def _warm_model_loading_imports() -> None:
    """`mlflow.sklearn` (and, transitively, torch's operator-overload registration and skops'
    type registry) is not imported until the first `ModelLoader.load()` call — cold, that import
    alone takes tens of seconds on this stack, which blows straight through
    `ModelLoader`'s per-request load timeout if it happens to land on the first prediction
    request. Paying that cost once here, in the background at startup, keeps it off every
    request's timeout budget instead. `torch` is imported alongside it for the same reason, now
    that `ModelLoader` also serves Hermes-RPT-0.1 (`hermes_rpt.inference.model_loading`)."""
    import mlflow.sklearn  # noqa: F401
    import torch  # noqa: F401


async def _warm_model_loading_imports_in_background(logger: Any) -> None:
    # Deliberately NOT awaited before `yield` — this import has measured 20-30s+ cold, which
    # exceeds Cloud Run's configured startup probe window (docker/cloudrun-service.yaml:
    # initialDelaySeconds 2 + periodSeconds 3 * failureThreshold 10 = 32s) and blocking on it
    # here made `/health/live` itself unavailable until the import finished, failing the probe
    # and the whole deployment outright — the exact liveness-vs-slow-unrelated-import problem
    # `/health/live`'s own docstring says it's designed to avoid. Run it as a background task
    # instead: `/health/live` (and everything else) is served immediately: worst case, a
    # prediction request that lands before this finishes just pays the cold-import cost itself,
    # same as before this warm-up existed at all — degraded, not broken.
    try:
        await asyncio.to_thread(_warm_model_loading_imports)
        logger.info("model_loading_imports_warmed")
    except Exception:
        logger.exception("model_loading_imports_warm_up_failed")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)
    logger.info("startup", environment=settings.environment, service=settings.service_name)
    warm_up_task = asyncio.create_task(_warm_model_loading_imports_in_background(logger))
    yield
    warm_up_task.cancel()
    logger.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Hermes-RPT API",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CorrelationIdMiddleware,
        request_id_header=settings.request_id_header,
        correlation_id_header=settings.correlation_id_header,
    )

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Exception handlers can't use FastAPI's Depends()/dependency_overrides machinery, so the
    # sessionmaker they use for best-effort audit writes is threaded through app.state instead
    # — tests override this the same way they'd override a dependency. See
    # apps/api/exception_handlers.py.
    app.state.db_sessionmaker = get_sessionmaker()

    register_exception_handlers(app)
    app.include_router(auth.router)
    app.include_router(memberships.router)
    app.include_router(connections.router)
    app.include_router(schema_discovery.router)
    app.include_router(mappings.router)
    app.include_router(features.router)
    app.include_router(predictions.router)
    app.include_router(monitoring.router)

    @app.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        """Liveness probe: the process is up and able to serve requests at all.
        Deliberately does not touch the database — see /health/ready for that."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def health_ready() -> dict[str, Any]:
        """Readiness probe: also confirms the control-plane database is reachable with a
        cheap `SELECT 1`. Never touches a customer/tenant database — those are per-tenant and
        have no meaning for a platform-wide readiness check (docs/ARCHITECTURE.md §1)."""
        checks: dict[str, str] = {}
        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks["control_plane_database"] = "ok"
        except Exception:  # noqa: BLE001 - readiness probe: any failure means "not ready"
            checks["control_plane_database"] = "unreachable"
            return {"status": "degraded", "checks": checks}
        return {"status": "ok", "checks": checks}

    @app.get("/version", tags=["health"])
    async def version() -> dict[str, Any]:
        settings = get_settings()
        return {
            "service": settings.service_name,
            "version": __version__,
            "environment": settings.environment,
            "uptime_seconds": round(time.monotonic() - _START_TIME, 3),
        }

    return app


app = create_app()
