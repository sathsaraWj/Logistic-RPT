# ADR-0001: Record architecture decisions

## Status

Accepted

## Context

Hermes-RPT will be built across many sequential phases, several by different contributors
(human or AI-assisted) over time. Decisions made early — especially security-relevant ones —
need to remain discoverable and their rationale needs to survive beyond the pull request that
made them.

## Decision

We will use lightweight Architecture Decision Records (ADRs) stored under `docs/adr/`, one file
per decision, numbered sequentially (`NNNN-title-in-kebab-case.md`), using the sections: Status,
Context, Decision, Consequences. ADRs are never renumbered or deleted; a superseded ADR is
marked "Superseded by ADR-XXXX" rather than removed.

## Consequences

* Every non-obvious or security-relevant architectural choice should get an ADR, not just a
  mention in a PR description or chat log.
* Future phases (and future readers) can answer "why is it built this way" without
  archaeology.
* Adds a small amount of process overhead per significant decision.
