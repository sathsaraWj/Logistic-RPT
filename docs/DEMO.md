# End-to-End Demonstration (Phase 17)

A local, two-tenant walkthrough of the whole platform — from provisioning a customer database
connection through prediction, schema drift, and tenant isolation — against real PostgreSQL
databases and the real HTTP API, not mocks. See [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md)
for the underlying security controls this demo exercises, and
[docs/CUSTOMER_DATABASE_GUIDE.md](CUSTOMER_DATABASE_GUIDE.md) for what a real customer would do
in place of `demo-seed`'s synthetic data generation.

## 1. What the two demo tenants look like

Both tenants represent the same five real ontology entities
([configs/ontology/v1.yaml](../configs/ontology/v1.yaml): `Vehicle`, `Driver`, `Trip`,
`Delivery`, `MaintenanceEvent`) through deliberately different physical schemas — the
heterogeneity the mapping layer (Phase 7) and ontology (Phase 6) exist to absorb:

| Ontology entity | Tenant Alpha table | Tenant Beta table |
|---|---|---|
| Vehicle | `fleet_vehicle` | `assets` |
| Driver | `fleet_driver` | `employees` |
| Trip | `transport_trip` | `jobs` |
| Delivery | `delivery_record` | `consignments` |
| MaintenanceEvent | `maintenance_log` | `service_orders` |

Column names differ too (e.g. Alpha's `transport_trip.planned_departure_at` is Beta's
`jobs.sched_depart`) — see [scripts/demo/fixtures.py](../scripts/demo/fixtures.py) for the
exact column-level mapping and `docker/postgres-fixtures/{alpha,beta}/init.sql` for the DDL.
Each tenant's "customer database" is a genuinely separate PostgreSQL instance
(`docker-compose.yml`'s `tenant-alpha-db`/`tenant-beta-db`), not just a separate schema on one
shared instance — the same isolation a real customer's own database would have.

## 2. Prerequisites

* Docker (for `docker-compose.yml`'s four services: control-plane DB, MLflow, Tenant Alpha DB,
  Tenant Beta DB).
* `make install-ml` (the API app imports MLflow/scikit-learn/PyTorch transitively through the
  predictions router — every demo step past `demo-seed` needs it).

**Port conflicts.** The stack defaults to ports 5432 (control-plane DB), 5433 (Tenant Alpha DB),
5434 (Tenant Beta DB), 5000 (MLflow) — the same defaults `make up`/`make test-integration` use.
If any of those are already taken by something else on your machine, override them; every
`demo-*` target and `docker-compose.yml` itself read the same four environment variables, so
setting them once is enough:

```bash
export CONTROL_PLANE_DB_PORT=25432
export TENANT_ALPHA_DB_PORT=25433
export TENANT_BETA_DB_PORT=25434
export MLFLOW_PORT=25000
```

## 3. Running the demonstration

```bash
make demo-up            # docker compose up -d, then alembic upgrade head
make demo-seed          # provision Alpha/Beta tenants; seed synthetic operational history
make demo-discover      # real schema discovery against both tenant databases
make demo-map           # create + activate schema mappings for all five entities, per tenant
make demo-train         # build tenant datasets; train baselines + Hermes-RPT-0.1 Tiny; promote
make demo-predict       # execute a real prediction per tenant; show its full lineage
make demo-security-test # drift, suspension, cross-tenant-access attempt, log-safety proof
make demo-down          # docker compose down
```

Run them in this order — each step depends on the state the previous one left in the (real,
persistent) control-plane database. `demo-seed` through `demo-security-test` are each a separate
process invocation (`uv run --group ml python -m scripts.demo <subcommand>`); nothing is held in
memory between them.

### Expected output, step by step

**`make demo-up`** — four containers healthy; `alembic upgrade head` reports the current
migration head with no errors.

**`make demo-seed`**
```
tenant demo-alpha: id=<uuid> user=admin@demo-alpha.example.com
tenant demo-beta: id=<uuid> user=admin@demo-beta.example.com
seeded demo-alpha: 4 vehicles, 3 drivers, 60 trips, ~50 deliveries, ~6 maintenance events
seeded demo-beta: 4 vehicles, 3 drivers, 60 trips, ~50 deliveries, ~6 maintenance events
```
Idempotent: re-running reuses the existing tenants (looked up by slug) rather than erroring or
duplicating them, but will insert a second batch of trips/deliveries — re-seed only if you
intend a larger dataset, or `make demo-down && make demo-up` first for a clean slate.

**`make demo-discover`** — for each tenant: registers a `primary` connection against its real
database (read-only-role credentials, same as [docs/CUSTOMER_DATABASE_GUIDE.md](CUSTOMER_DATABASE_GUIDE.md)
describes), validates and enables it, runs real introspection, and prints the discovered table
names plus a schema fingerprint. Alpha's and Beta's fingerprints are different — proof discovery
captured the real, distinct schemas rather than something shared/cached between tenants.

**`make demo-map`** — for each tenant, for each of the five entities: creates a draft mapping
from the fixtures in [scripts/demo/fixtures.py](../scripts/demo/fixtures.py), submits it for
validation, approves it, and activates it. Prints `<entity> -> <table> (active)` five times per
tenant.

**`make demo-train`** — for each tenant: builds a `delivery-delay-risk` dataset from real
extracted features (via the mapped `Trip` entity and its related `Vehicle`/`Delivery`/
`MaintenanceEvent` data — `RouteStop`/`OdometerReading`/`FuelEvent` features come back missing
since this demo's five-entity schema doesn't map those three, handled gracefully per each
feature's declared `missing_value_behavior`, never a hard failure), trains all three baselines
plus Hermes-RPT-0.1 Tiny, and promotes the strongest model to `PRODUCTION` for that tenant. Note
per [docs/HERMES_RPT_0_1.md](HERMES_RPT_0_1.md): a Hermes-RPT win here is not a production-
readiness claim, just what this particular synthetic dataset happened to produce.

**`make demo-predict`** — for each tenant: picks a real, already-completed trip from that
tenant's own database and calls `POST /v1/predictions/delivery-delay` for it, printing the delay
probability, risk level, and a full lineage chain (model version + stage + artifact checksum,
mapping version, feature contract version, explanation count) pulled from the real
`PredictionResult` row — not a fabricated summary.

**`make demo-security-test`** — five checks, all against the real stack:
1. Directly `ALTER TABLE fleet_vehicle RENAME COLUMN registration_no TO reg_no` on Alpha's
   database — simulating the customer changing their own schema out of band.
2. Re-runs discovery for Alpha; the new snapshot's `drift_summary` reports the rename, and
   `MappingService.suspend_affected_by_drift` suspends Alpha's `Vehicle` mapping (never
   silently rewrites it).
3. Runs a Beta prediction — succeeds, unaffected, proving the drift is scoped to the tenant (and
   the specific mapping) it actually hit.
4. Uses Alpha's own token to `GET` Beta's connection and mapping by ID — both return `404`, not
   `403` (ADR-0003: a cross-tenant probe can't distinguish "doesn't exist" from "exists but
   isn't yours").
5. Captures structured log output during a connection operation and asserts Beta's/Alpha's
   database password never appears in it.

Exits non-zero if any of the five checks fails, and prints `Security test PASSED`/`FAILED`
accordingly.

## 4. Where reports are written

Every step writes a JSON report to `data/demo_runs/<run-timestamp>/<step>.json` — aggregate
facts only (row counts, model metrics, prediction IDs, pass/fail booleans, table names,
fingerprints), matching this whole platform's "no raw customer values in metadata" posture (same
rule `scripts/build_synthetic_dataset.py` follows). `data/` is gitignored; these reports are
never committed. No credential value, connection string, or raw database row ever appears in a
report or in this demo's own stdout.

## 5. Cleaning up

```bash
make demo-down
```

Removes the containers; named Docker volumes persist (matching `make down`'s existing behavior)
— `docker volume rm hermes-rpt_hermes_control_db_data hermes-rpt_hermes_tenant_alpha_data
hermes-rpt_hermes_tenant_beta_data hermes-rpt_hermes_mlflow_data` for a genuinely clean slate.

## 6. Design notes

* **Real HTTP, real auth, real RLS.** `demo-discover`/`demo-map`/`demo-predict`/
  `demo-security-test` drive the actual FastAPI app (`TestClient(create_app())`) with real
  signed dev JWTs (`hermes_rpt.auth.dev_tokens.issue_dev_token`) — the same request path a real
  client hits, not a shortcut around auth/authorization. `demo-train` calls the training/registry
  services directly (there is no HTTP endpoint for training — see
  [apps/api/routers/](../apps/api/routers/); that's a deliberate scope boundary, matching
  `apps/trainer/main.py`'s existing CLI-only convention) but explicitly binds Row-Level Security
  on every session it opens (`scripts/demo/__main__.py`'s `_rls_bound_session`), the same fix
  Phase 16 applied to `hermes_rpt.schemas.jobs.run_discovery_job` — this demo runs against a real
  PostgreSQL control plane, unlike `scripts/build_synthetic_dataset.py`'s ephemeral SQLite one,
  so RLS is genuinely enforced here, not a no-op.
* **Why a different demo fixture from `scripts/build_synthetic_dataset.py`.** That script (Phase
  9) intentionally stays fast and dependency-free (in-memory SQLite control plane and "customer"
  databases) for quick local dataset-building iteration. This demo deliberately trades that speed
  for realism: real separate PostgreSQL instances per tenant, the real HTTP/auth/RLS stack, and
  the exact five-table schema prompts.txt Prompt 17 specifies.
* **Seeding writes directly to the tenant databases**, bypassing `hermes_rpt`'s own connector —
  that connector is read-only by design
  (`hermes_rpt.connectors.postgres`/`query_guard.assert_read_only_statement`) and was never meant
  to write to a customer's database. `scripts/demo/seed.py` stands in for "the customer's own
  systems, which already populated this data before Hermes-RPT ever connected."
