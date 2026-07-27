# Schema Mapping

Status: implements Phase 7 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 5
(discovery, `docs/SCHEMA_DISCOVERY` — see `hermes_rpt.schemas`) and Phase 6
([ONTOLOGY.md](ONTOLOGY.md)). See
[adr/0007-deterministic-mapping-suggestions-before-llm.md](adr/0007-deterministic-mapping-suggestions-before-llm.md).

## 1. What a mapping document looks like

```yaml
entity: Vehicle
source:
  schema: public
  table: fleet_vehicle
identity:
  vehicle_id:
    sources: [{ column: vehicle_id }]
fields:
  registration_number:
    sources: [{ column: registration_no }]
  current_odometer_km:
    sources: [{ column: odometer_km }]
```

`hermes_rpt.mappings.document.MappingDocument` is the Pydantic schema this validates against.
Every `FieldMapping.sources` entry is one of: a direct `column`, a `static` value, or a
`derived` expression (`hermes_rpt.mappings.expressions`) — never arbitrary Python or SQL.
`sources` is a priority-ordered list (source-priority rules): the first source that resolves to
a non-null value wins. A `column` source can also carry `source_unit` (unit conversion),
`source_timezone` (timestamp transformation), `value_map` (enumerated-value mapping), and
`null_values` (null handling).

## 2. The safe expression language

Derived fields use a **closed set of structured node types** — `column`, `literal`, `concat`,
`coalesce`, `upper`, `lower`, `trim`, `if_null` — never a string that gets parsed or `eval`'d.
See `hermes_rpt.mappings.expressions` for the complete list; there is no mechanism to add a new
node kind from a mapping document itself.

## 3. Lifecycle

```text
draft --submit_for_validation--> pending_validation --approve--> approved --activate--> active
  ^                                                                                        |
  |                                                                                        v
  +-------------------------------- create_new_version --------------------------- (stays active)
  |
  +--reject--> rejected                                          active --deprecate--> deprecated
```

* A mapping always belongs to one tenant, references one `SchemaSnapshot`, and one ontology
  version (`SchemaMapping.ontology_version`).
* **Immutable after activation**: once a `MappingVersion` is pointed to by
  `SchemaMapping.active_version_id`, no code path mutates its `mapping_document` again. Any
  change is a new `MappingVersion` row (`MappingService.create_new_version`), which must
  independently pass validation and approval before it can replace the active pointer.
* **Only approved, active, non-drift-suspended mappings are usable in production** —
  `MappingService.get_production_version` is the single method that enforces this; nothing else
  in the platform reads a mapping's `mapping_document` directly.
* **Drift suspends, never rewrites**: `MappingService.suspend_affected_by_drift`, called after a
  new schema snapshot completes (Phase 5), flips `suspended_due_to_drift` on any active mapping
  whose source table was affected — the mapping's `state` stays `active`, only production
  usability is blocked, and only a human creating and activating a new version clears it.

## 4. Deterministic mapping suggestions

`hermes_rpt.mappings.suggest.suggest_mapping` proposes a draft `MappingDocument` from a
discovered table and an ontology entity using name-similarity, data-type compatibility, primary-
key/comment/profiling signals — **no LLM call**. Every suggestion carries a `confidence` score
and human-readable `explanation`; a suggestion is exactly as subject to the human-approval
lifecycle above as a hand-authored mapping (nothing skips `approve`/`activate` just because
`is_ai_suggested` might one day be true for an LLM-based successor).

Concretely, from the two synthetic fixtures used throughout this repo:

| Ontology field | Tenant Alpha (`fleet_vehicle`) | Tenant Beta (`assets`) |
|---|---|---|
| `vehicle_id` | `vehicle_id` (exact name + primary key) | `asset_id` (primary key, weak name match) |
| `registration_number` | `registration_no` | `tag` (usually too weak a match to suggest) |
| `current_odometer_km` | `odometer_km` | `total_km` |

Beta's `asset_id` still gets identified as the `vehicle_id` mapping candidate primarily because
it is the table's *primary key*, not because of name similarity — proof the engine adapts to a
schema that shares almost no vocabulary with Alpha's.

## 5. What Phase 7 does not do

Compiling an active mapping into a real, executable, allowlist-checked query is Phase 8's "safe
query compiler" — this phase only defines and governs the mapping *document*, and validates it
structurally/semantically against a schema snapshot's already-discovered metadata.
