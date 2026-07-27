"""Hermes-RPT training entry point.

Placeholder in Phase 1. Baseline and Hermes-RPT training loops (Phases 10-12) run as
`uv run --group ml python -m apps.trainer.main <task>` once the `ml` dependency group
(torch, mlflow, scikit-learn) is installed.
"""

from __future__ import annotations

from hermes_rpt.common.logging import configure_logging, get_logger
from hermes_rpt.common.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)
    logger.info(
        "trainer_placeholder_start",
        environment=settings.environment,
        note="no training tasks registered yet; see docs/IMPLEMENTATION_PLAN.md phases 10-12",
    )


if __name__ == "__main__":
    main()
