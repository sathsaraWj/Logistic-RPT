# ADR-0007: Mapping suggestions start deterministic; no LLM-generated mappings until human-approval workflow exists and is proven

## Status

Accepted

## Context

Suggesting how a customer's schema maps to the canonical ontology is a natural place to apply an
LLM (semantic column-name matching, business-term inference). But an LLM-generated mapping that
is wrong in a subtle way (e.g. mapping "gross weight" to "net weight," or silently choosing the
wrong join path) can corrupt every downstream feature, dataset, and prediction for that tenant,
and mapping definitions are also a potential injection surface if suggestions are trusted too
readily.

## Decision

Phase 7 implements a deterministic mapping-suggestion engine only: normalized column-name
similarity, data-type compatibility, key-relationship matching, table/column comments, and safe
profile statistics. No external LLM call is made to generate mappings in this phase. Every
mapping — deterministic-suggested or (in a later phase) AI-suggested — carries confidence and
explanation fields and requires explicit human approval before it can become `active`; mappings
are immutable once active, and changes create a new version. The plumbing for AI-assisted
suggestions (confidence/explanation fields, mandatory human approval) is built now specifically
so that adding an LLM-based suggester later is additive, not a redesign.

## Consequences

* Slower/lower-recall initial mapping suggestions than an LLM could provide, but with a fully
  auditable, deterministic rationale for every suggestion.
* No prompt-injection surface from customer schema metadata (table/column comments, names) into
  a model that could influence mapping decisions, because no such model is called yet.
* When an LLM-assisted suggester is added later, it plugs into the same confidence/explanation/
  human-approval contract rather than requiring new governance to be invented under pressure.
