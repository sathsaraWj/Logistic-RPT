"""Risk-level bucketing (Phase 13) — a fixed, documented mapping from a probability to the
`"low"/"medium"/"high"` label the API response exposes. Deliberately not tenant- or model-
configurable in this phase: a fixed threshold is simpler to reason about and test than a
per-tenant policy, and nothing in prompts.txt asks for the latter.
"""

from __future__ import annotations

_MEDIUM_THRESHOLD = 0.3
_HIGH_THRESHOLD = 0.6


def risk_level_for(probability: float) -> str:
    if probability >= _HIGH_THRESHOLD:
        return "high"
    if probability >= _MEDIUM_THRESHOLD:
        return "medium"
    return "low"
