"""Safe explanation generation (Phase 13) — "generate safe explanation" in the inference flow.

An explanation here is *never* more than a feature name (already-sanitized ontology field names
— see `hermes_rpt.features.contract`, never a raw source column or value) plus a qualitative
`"increases_risk"` / `"decreases_risk"` direction. There is no magnitude, no raw value, no causal
language — "do not expose ... unsupported causal claims" — this reports a *correlational*
contribution to the model's own score, not a claim about what caused a delay.

Only computed for models that expose `feature_importance()` (Phase 10's baselines with linear
coefficients — currently just logistic regression); every other model kind returns no
explanations at all rather than fabricating one it cannot honestly support.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

_MAX_EXPLANATIONS = 3


class PredictionExplanation(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor: str
    direction: str  # "increases_risk" | "decreases_risk"


def generate_explanations(
    *, feature_importance: dict[str, float] | None, features: dict[str, float | int | bool | None]
) -> tuple[PredictionExplanation, ...]:
    if not feature_importance:
        return ()

    contributions: list[tuple[str, float]] = []
    for name, coefficient in feature_importance.items():
        value = features.get(name)
        if value is None:
            continue
        contributions.append((name, coefficient * float(value)))

    contributions.sort(key=lambda item: abs(item[1]), reverse=True)
    return tuple(
        PredictionExplanation(
            factor=name, direction="increases_risk" if contribution > 0 else "decreases_risk"
        )
        for name, contribution in contributions[:_MAX_EXPLANATIONS]
        if contribution != 0
    )
