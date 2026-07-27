-- Tenant Beta simulated customer database. Same underlying fleet concept as Tenant Alpha
-- (docker/postgres-fixtures/alpha/init.sql), intentionally different table/column names.

CREATE TABLE IF NOT EXISTS assets (
    asset_id     SERIAL PRIMARY KEY,
    tag          VARCHAR(20) NOT NULL,
    model_name   VARCHAR(100),
    total_km     NUMERIC(10, 1) NOT NULL DEFAULT 0
);

INSERT INTO assets (tag, model_name, total_km) VALUES
    ('BETA-A1', 'Volvo FH16', 88210.0),
    ('BETA-A2', 'Scania R450', 55302.7);
