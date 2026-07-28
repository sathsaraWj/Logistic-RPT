# ADR-0008: Ship tabular baselines before the relational transformer; the transformer must beat them to justify itself

## Status

Accepted

## Context

Hermes-RPT's headline research artifact is a relational transformer, but a transformer is more
expensive to build, train, and operate than a tabular baseline, and there is no a priori
guarantee it will outperform a strong baseline on any given customer's data — especially early,
with limited history and synthetic data. Committing to the transformer as the only prediction
path risks shipping a system that is worse than a simple, well-understood model while being
harder to explain and debug.

## Decision

Phase 10 (baselines: logistic regression, gradient-boosted trees, small MLP) is built and
evaluated on the exact same feature contract and dataset before Phase 11 (Hermes-RPT v0.1)
begins. The inference API (Phase 13) is designed so it can serve a baseline model without
requiring the transformer to exist. The transformer is evaluated against the strongest baseline
using the same metrics and honest reporting requirements (Phase 11 explicitly disallows treating
falling training loss alone as evidence of success). Only the "Tiny" transformer configuration is
trained initially; "Small" and "Base experimental" remain config-only until there's a concrete
reason to invest further compute.

## Consequences

* The platform has a working, explainable, comparatively cheap prediction path from Phase 10
  onward, independent of how the transformer research turns out.
* Model-registry, feature-contract, and inference-API work is validated against a simple model
  first, reducing the number of moving parts that could hide a bug when the more complex
  transformer is introduced.
* Creates an explicit, falsifiable bar (beat the baseline meaningfully) rather than an assumed
  narrative that the transformer is automatically the better choice.

## Update (Phase 19, 2026-07-28)

The bar this ADR set was actually cleared. The original synthetic label generator produced a
delay outcome statistically independent of every feature, so no comparison run — before Phase 19
— could be more than noise (`docs/MODEL_RESEARCH_PLAN.md` §9, original text, preserved above).
Phase 19 made the label risk-weighted by distance/vehicle-age/breakdown/route/driver history
(`docs/BASELINE_MODELS.md` §10) specifically to make an honest comparison possible, then re-ran
it: `hermes-rpt-0.1-tiny-scratch` beat every Phase 10 baseline on PR-AUC, consistently across
three different training seeds (0.3053–0.3633 vs. the strongest baseline's 0.2793 — full table in
`docs/MODEL_RESEARCH_PLAN.md` §9's Phase 19 follow-up). Per the decision this ADR describes, it
was promoted to `PRODUCTION` via `ModelRegistryService.transition_stage`.

This is a real result, not a formality — but it's still a synthetic-data result with
deliberately-injected correlation, not evidence about real fleet operations. The production
blocker in `docs/RELEASE_READINESS.md` §6 item 2 (no real customer data has validated any
model-quality claim) is unaffected by this update and applies to both model families equally.
