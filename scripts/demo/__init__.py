"""Phase 17 end-to-end local demonstration: two synthetic tenants (Alpha, Beta), each with a
real PostgreSQL "customer database" (`docker-compose.yml`'s `tenant-alpha-db`/`tenant-beta-db`,
seeded by `docker/postgres-fixtures/{alpha,beta}/init.sql`) using deliberately different table
and column names for the same five underlying fleet concepts. See `docs/DEMO.md` for the exact
`make demo-*` commands and what each one proves.
"""
