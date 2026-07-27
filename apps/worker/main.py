"""Hermes-RPT background worker entry point.

Placeholder in Phase 1: logs startup and exits cleanly. Real job handlers (schema discovery,
dataset builds, monitoring rollups) are added starting Phase 5, running against this same
process shape (`uv run python -m apps.worker.main`).
"""

from __future__ import annotations

from hermes_rpt.common.logging import configure_logging, get_logger
from hermes_rpt.common.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)
    logger.info(
        "worker_placeholder_start",
        environment=settings.environment,
        note="no job handlers registered yet; see docs/IMPLEMENTATION_PLAN.md phases 5, 9, 15",
    )


if __name__ == "__main__":
    main()
