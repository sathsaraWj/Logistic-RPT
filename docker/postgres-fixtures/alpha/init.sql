-- Tenant Alpha simulated customer database. Deliberately different table/column names from
-- Tenant Beta (docker/postgres-fixtures/beta/init.sql) even though both represent the same
-- underlying fleet concept — this is the heterogeneity the schema mapping layer (Phase 7) and
-- ontology (Phase 6) exist to absorb. Minimal on purpose for Phase 4 (connector) tests; Phase 5/
-- 9/17 build this out further.

CREATE TABLE IF NOT EXISTS fleet_vehicle (
    vehicle_id      SERIAL PRIMARY KEY,
    registration_no VARCHAR(20) NOT NULL,
    model           VARCHAR(100),
    odometer_km     NUMERIC(10, 1) NOT NULL DEFAULT 0
);

INSERT INTO fleet_vehicle (registration_no, model, odometer_km) VALUES
    ('ALPHA-001', 'Ford Transit', 42150.5),
    ('ALPHA-002', 'Mercedes Sprinter', 18730.2);
