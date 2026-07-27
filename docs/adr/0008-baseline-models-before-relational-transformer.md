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
