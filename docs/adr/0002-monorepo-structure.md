# ADR-0002: Single monorepo with apps/ and src/hermes_rpt/ split

## Status

Accepted

## Context

Hermes-RPT has three runtime processes (API, worker, trainer) that share a large amount of
domain logic (tenant resolution, connectors, mappings, feature extraction, model registry
access) and must stay in lockstep on data models, ontology versions, and security invariants.
Splitting these into separate repositories early would duplicate that shared logic or force a
published-package release cycle before the design has stabilized.

## Decision

Use a single monorepo. Thin, deployable entry points live under `apps/{api,worker,trainer}/`;
all shared domain logic lives in an installable library package at `src/hermes_rpt/`, organized
by bounded-context subpackage (`auth`, `tenants`, `connectors`, `secrets`, `schemas`,
`mappings`, `ontology`, `features`, `datasets`, `models`, `training`, `inference`, `registry`,
`audit`, `monitoring`, `common`). Tests mirror this structure under `tests/{unit,integration,
security,model}/`.

## Consequences

* A single dependency/version source of truth (`pyproject.toml`) and a single CI pipeline for
  the whole platform in these early phases.
* Shared invariants (e.g. tenant isolation) are enforced once in `src/hermes_rpt` and consumed
  by all three apps, rather than reimplemented per service.
* If/when a subsystem needs independent scaling or a separate release cadence (most likely
  `apps/trainer`, which may need GPU infrastructure), it can be split out later; this ADR does
  not block that, it just defers it until there's a concrete need.
* Risk: a monorepo can accumulate coupling if package boundaries inside `src/hermes_rpt` aren't
  respected. Mitigated by keeping API handlers thin and routing all data access through
  repository/service layers (ADR-0003).
