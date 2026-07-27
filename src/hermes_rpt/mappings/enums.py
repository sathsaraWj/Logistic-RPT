from __future__ import annotations

from enum import StrEnum


class MappingState(StrEnum):
    """Lifecycle states from prompts.txt Phase 7. Transition logic (which states can move to
    which) is implemented in Phase 7 — Phase 2 only persists the state."""

    DRAFT = "draft"
    PENDING_VALIDATION = "pending_validation"
    APPROVED = "approved"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"
