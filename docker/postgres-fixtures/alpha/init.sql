-- Tenant Alpha simulated customer database (Phase 17 end-to-end demo). Five tables, deliberately
-- different table/column names and terminology from Tenant Beta
-- (docker/postgres-fixtures/beta/init.sql) even though both represent the same underlying fleet
-- concepts (Vehicle, Driver, Trip, Delivery, MaintenanceEvent — see configs/ontology/v1.yaml) —
-- this heterogeneity is exactly what the schema mapping layer (Phase 7) and ontology (Phase 6)
-- exist to absorb. DDL only, no seed rows: `scripts/demo` seeds synthetic operational history at
-- `make demo-seed` time so dates stay relative to when the demo is actually run.

CREATE TABLE IF NOT EXISTS fleet_vehicle (
    vehicle_id          VARCHAR(50) PRIMARY KEY,
    registration_no     VARCHAR(20) NOT NULL,
    make                VARCHAR(100),
    model_name          VARCHAR(100),
    manufacture_year    INTEGER,
    odometer_km         NUMERIC(10, 1) NOT NULL DEFAULT 0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- TIMESTAMPTZ, not DATE, even though the ontology's Vehicle.acquired_at is logical_type
    -- "date" — hermes_rpt.features.derive._coerce_datetime (used by the delivery-delay-risk
    -- feature contract's vehicle-age derived feature) only handles a full datetime, the same
    -- reason hermes_rpt.synthetic.schemas.VEHICLE already uses TIMESTAMP here rather than DATE.
    acquired_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fleet_driver (
    driver_id       VARCHAR(50) PRIMARY KEY,
    full_name       VARCHAR(200) NOT NULL,
    license_number  VARCHAR(50),
    license_expiry  DATE,
    hired_at        DATE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS transport_trip (
    trip_id                 VARCHAR(50) PRIMARY KEY,
    vehicle_id              VARCHAR(50) NOT NULL REFERENCES fleet_vehicle(vehicle_id),
    driver_id               VARCHAR(50) REFERENCES fleet_driver(driver_id),
    route_id                VARCHAR(50),
    planned_departure_at    TIMESTAMPTZ NOT NULL,
    planned_arrival_at      TIMESTAMPTZ,
    actual_departure_at     TIMESTAMPTZ,
    actual_arrival_at       TIMESTAMPTZ,
    planned_distance_km     NUMERIC(8, 1),
    status                  VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_record (
    delivery_id     VARCHAR(50) PRIMARY KEY,
    trip_id         VARCHAR(50) NOT NULL REFERENCES transport_trip(trip_id),
    order_id        VARCHAR(50),
    planned_at      TIMESTAMPTZ NOT NULL,
    delivered_at    TIMESTAMPTZ,
    status          VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_log (
    maintenance_event_id   VARCHAR(50) PRIMARY KEY,
    vehicle_id             VARCHAR(50) NOT NULL REFERENCES fleet_vehicle(vehicle_id),
    work_order_id          VARCHAR(50),
    event_type              VARCHAR(20) NOT NULL,
    started_at              TIMESTAMPTZ NOT NULL,
    completed_at            TIMESTAMPTZ
);
