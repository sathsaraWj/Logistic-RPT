# The Canonical Hermes Ontology

Status: implements Phase 6 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). See
[adr/0006-canonical-ontology-decoupled-from-customer-schemas.md](adr/0006-canonical-ontology-decoupled-from-customer-schemas.md)
for why this exists as a separate layer at all.

## 1. What this is

A versioned, customer-agnostic vocabulary of fleet/logistics concepts — `Vehicle`, `Trip`,
`FuelEvent`, and so on — that every tenant's data eventually gets mapped onto (Phase 7),
regardless of how their own database happens to be shaped. The ontology itself never contains a
customer table or column name.

The ontology is defined once, in YAML (`configs/ontology/v1.yaml`), and everything else —
Pydantic runtime models, validation — is derived from that file
(`hermes_rpt.ontology.registry`), so the field list for `Vehicle` exists in exactly one place.

## 2. The 28 entities

| Domain | Entities |
|---|---|
| Fleet | Vehicle, VehicleType, VehicleAssignment, OdometerReading, VehicleAvailability |
| Workforce | Driver, Operator, Shift, DriverAssignment, SafetyEvent |
| Operations | Trip, Route, RouteStop, Delivery, Order, Package, Customer |
| Maintenance | MaintenanceEvent, WorkOrder, Fault, SparePart, InventoryTransaction, Tyre, TyreInstallation |
| Cost and consumption | FuelEvent, TripExpense, MaintenanceCost, TollEvent |

## 3. Field semantics

* **Distance** is always kilometres internally (`unit: kilometres`); **volume** is always
  litres (`unit: litres`); **mass** is always kilograms (`unit: kilograms`). Source-unit
  conversion (miles, US/UK gallons, pounds) happens in the unit conversion layer
  (`hermes_rpt.ontology.units`), which a schema mapping (Phase 7) calls — the ontology field
  itself never changes meaning.
* **Money** is never a bare number — every monetary field is a `Money {amount, currency_code}`
  value (`hermes_rpt.ontology.values.Money`). There is no cross-currency conversion layer:
  exchange rates are time-varying market data, not a fixed physical constant like a kilometre.
* **Datetimes** are a `CanonicalDatetime {utc, original_timezone}` value
  (`hermes_rpt.ontology.values.CanonicalDatetime`), never a bare timestamp — `utc` is always
  timezone-aware and normalized, `original_timezone` (an IANA zone name) is preserved
  separately when the source recorded one.
* **Business identifiers** (every `*_id` field) are always `string` — never a numeric surrogate
  — enforced by `OntologyFieldDefinition` validation, not just convention.
* **Missing values** are always an explicit `None` on optional fields (Pydantic's `T | None`),
  never a sentinel like `-1` or `""`. Required fields have no default at all: a missing value
  there is a validation error, not silently accepted.
* **Personally identifiable fields** carry a `classification` label (`public`, `internal`,
  `pii`, `sensitive_pii` — `hermes_rpt.ontology.values.DataClassification`) — e.g.
  `Driver.full_name` is `pii`, `Driver.license_number` is `sensitive_pii`.

## 4. Relationships

Each entity's `relationships` list names a related entity, a cardinality
(`one_to_one`/`one_to_many`/`many_to_one`/`many_to_many`), and whether the relationship is
required. `OntologyDefinition` validation rejects a relationship whose `target_entity` doesn't
exist — the ontology file cannot reference an entity that isn't defined in it.

## 5. Example: two different source schemas, one canonical entity

Tenant Alpha and Tenant Beta (the synthetic fixtures used throughout this repo — see
`docker/postgres-fixtures/{alpha,beta}/init.sql`) both track vehicles, with deliberately
different table and column names:

| Concept | Tenant Alpha (`fleet_vehicle`) | Tenant Beta (`assets`) | Canonical `Vehicle` field |
|---|---|---|---|
| Identifier | `vehicle_id` | `asset_id` | `vehicle_id` (string) |
| Registration | `registration_no` | `tag` | `registration_number` |
| Model | `model` | `model_name` | `model_name` |
| Distance | `odometer_km` (already km) | `total_km` (already km) | `current_odometer_km` (km) |

Both `fleet_vehicle.odometer_km` and `assets.total_km` happen to already be kilometres in this
fixture; a real customer storing odometer readings in miles would have that conversion applied
by their `SchemaMapping` (Phase 7), via `hermes_rpt.ontology.units.convert_distance`, on the way
into `current_odometer_km` — the canonical field is defined in kilometres regardless of what
unit any particular source happens to use.

This table is illustrative of what a Phase 7 `SchemaMapping` document produces, not something
Phase 6 executes — no mapping engine exists yet at this phase (see
[docs/adr/0006-canonical-ontology-decoupled-from-customer-schemas.md](adr/0006-canonical-ontology-decoupled-from-customer-schemas.md)).

## 6. Versioning

`OntologyDefinition.version` (currently `"1"`) is referenced by `SchemaMapping.ontology_version`
(Phase 2) so a mapping is always pinned to the ontology shape it was created against; a future
breaking ontology change ships as `configs/ontology/v2.yaml` rather than mutating `v1.yaml` in
place, so existing mappings keep working against the version they were built for.
