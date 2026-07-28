-- Tenant Beta simulated customer database (Phase 17 end-to-end demo). Same five underlying fleet
-- concepts as Tenant Alpha (docker/postgres-fixtures/alpha/init.sql), intentionally different
-- table/column names and terminology. DDL only, no seed rows: `scripts/demo` seeds synthetic
-- operational history at `make demo-seed` time.

CREATE TABLE IF NOT EXISTS assets (
    asset_id            VARCHAR(50) PRIMARY KEY,
    tag                 VARCHAR(20) NOT NULL,
    brand               VARCHAR(100),
    model               VARCHAR(100),
    year_built          INTEGER,
    total_km            NUMERIC(10, 1) NOT NULL DEFAULT 0,
    active_flag         BOOLEAN NOT NULL DEFAULT TRUE,
    -- TIMESTAMPTZ, not DATE — see docker/postgres-fixtures/alpha/init.sql's identical note on
    -- fleet_vehicle.acquired_at.
    in_service_since    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS employees (
    employee_id     VARCHAR(50) PRIMARY KEY,
    name             VARCHAR(200) NOT NULL,
    licence_no       VARCHAR(50),
    licence_expiry   DATE,
    start_date       DATE,
    active           BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS jobs (
    job_ref          VARCHAR(50) PRIMARY KEY,
    asset_ref        VARCHAR(50) NOT NULL REFERENCES assets(asset_id),
    driver_ref       VARCHAR(50) REFERENCES employees(employee_id),
    path_ref         VARCHAR(50),
    sched_depart     TIMESTAMPTZ NOT NULL,
    sched_arrive     TIMESTAMPTZ,
    real_depart      TIMESTAMPTZ,
    real_arrive      TIMESTAMPTZ,
    km_planned       NUMERIC(8, 1),
    job_status       VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS consignments (
    consignment_id      VARCHAR(50) PRIMARY KEY,
    job_ref             VARCHAR(50) NOT NULL REFERENCES jobs(job_ref),
    order_ref           VARCHAR(50),
    due_at              TIMESTAMPTZ NOT NULL,
    done_at             TIMESTAMPTZ,
    consignment_status  VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS service_orders (
    service_order_id    VARCHAR(50) PRIMARY KEY,
    asset_ref           VARCHAR(50) NOT NULL REFERENCES assets(asset_id),
    wo_ref               VARCHAR(50),
    service_type         VARCHAR(20) NOT NULL,
    opened_at            TIMESTAMPTZ NOT NULL,
    closed_at            TIMESTAMPTZ
);
