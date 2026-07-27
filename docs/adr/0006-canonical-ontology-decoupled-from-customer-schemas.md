# ADR-0006: Canonical ontology is customer-agnostic; per-customer mapping is a separate, versioned artifact

## Status

Accepted

## Context

Every Hermes customer's database differs in engine, table names, column names, relationships,
data quality, terminology, and history depth. If the ontology (and, worse, the model) encoded
any specific customer's table/column names, every new customer would require modelling changes,
and cross-customer code reuse would collapse.

## Decision

The canonical Hermes ontology (`src/hermes_rpt/ontology`, Phase 6) defines entities, fields,
units, types, relationships, and classification labels with no reference to any customer's
actual schema. Each tenant gets its own versioned `SchemaMapping` (Phase 7) translating its
`SchemaSnapshot` into the ontology through a restricted declarative mapping language. Feature
extraction, dataset building, and the model layer all operate purely in ontology terms; they
never see raw customer table/column names directly, only the mapped/canonical values.

## Consequences

* Onboarding a new customer is a mapping exercise (write/validate a `SchemaMapping`), not a code
  or model change.
* The ontology itself becomes a shared, carefully-versioned asset — a breaking ontology change
  affects every tenant's mappings and must be versioned and rolled out deliberately (mappings
  reference an explicit ontology version, Phase 7).
* Some fidelity is necessarily lost for any customer whose data doesn't fit the canonical shape
  well; the mapping language's optional/required field metadata and explicit missing-value
  representation (Phase 6) are the intended release valve, not ad hoc customer-specific code.
