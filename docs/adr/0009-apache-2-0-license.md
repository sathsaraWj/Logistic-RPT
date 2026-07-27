# ADR-0009: License the repository under Apache License 2.0

## Status

Accepted

## Context

Phase 1 requires recording the licence decision in an ADR before adding a licence file. A
permissive, patent-grant-inclusive licence is appropriate for a platform that may eventually
combine internal and third-party contributions and needs a clear, well-understood patent
grant given it touches ML model code.

## Decision

Hermes-RPT is licensed under the Apache License, Version 2.0. The `LICENSE` file at the
repository root contains the full licence text. This choice was already reflected by the
`LICENSE` file present at repository initialization; this ADR formally records the rationale
so the decision is discoverable rather than assumed.

## Consequences

* Contributions and dependencies should be checked for Apache-2.0 compatibility going forward.
* The explicit patent grant in Apache-2.0 is a deliberate choice given the project includes
  novel model architecture work (Hermes-RPT, Phase 11+).
* Any future change of licence would itself need a new ADR explaining the change, per
  ADR-0001.
