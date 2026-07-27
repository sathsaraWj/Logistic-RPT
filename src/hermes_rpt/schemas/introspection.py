"""PostgreSQL schema introspection.

Discovers only approved metadata (Phase 5 requirement): schema/table/view/column shape, keys,
constraints, indexes, approximate row counts, and comments. Never reads row *data* here — that
is exactly what optional profiling (`hermes_rpt.schemas.profiling`) does, deliberately
separately, off by default, and limited.

Only introspects schemas on `CustomerDatabaseConnection.schema_allowlist` — an empty allowlist
means nothing is introspected (fail closed, matching `hermes_rpt.connectors.query_guard`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class ColumnMetadata(BaseModel):
    name: str
    sql_type: str
    is_nullable: bool
    ordinal_position: int
    comment: str | None = None


class ForeignKeyMetadata(BaseModel):
    constraint_name: str
    columns: list[str]
    referenced_schema: str
    referenced_table: str
    referenced_columns: list[str]


class UniqueConstraintMetadata(BaseModel):
    constraint_name: str
    columns: list[str]


class IndexMetadata(BaseModel):
    name: str
    columns: list[str]
    is_unique: bool


class TableMetadata(BaseModel):
    schema_name: str
    name: str
    kind: str  # "table" or "view"
    comment: str | None = None
    columns: list[ColumnMetadata] = Field(default_factory=list)
    primary_key_columns: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyMetadata] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraintMetadata] = Field(default_factory=list)
    indexes: list[IndexMetadata] = Field(default_factory=list)
    approximate_row_count: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"


class SchemaIntrospectionResult(BaseModel):
    schemas: list[str]
    tables: list[TableMetadata] = Field(default_factory=list)


# --- SQL, parameterized, against approved schemas only --------------------------------------

_TABLES_SQL = text(
    """
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = ANY(:schemas)
    ORDER BY table_schema, table_name
    """
)

_COLUMNS_SQL = text(
    """
    SELECT table_schema, table_name, column_name, data_type, is_nullable, ordinal_position
    FROM information_schema.columns
    WHERE table_schema = ANY(:schemas)
    ORDER BY table_schema, table_name, ordinal_position
    """
)

_PRIMARY_KEYS_SQL = text(
    """
    SELECT tc.table_schema, tc.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = ANY(:schemas)
    ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
    """
)

_FOREIGN_KEYS_SQL = text(
    """
    SELECT
        tc.table_schema, tc.table_name, tc.constraint_name, kcu.column_name,
        ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, ccu.column_name AS ref_column
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = ANY(:schemas)
    ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position
    """
)

_UNIQUE_CONSTRAINTS_SQL = text(
    """
    SELECT tc.table_schema, tc.table_name, tc.constraint_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = ANY(:schemas)
    ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position
    """
)

_INDEXES_SQL = text(
    """
    SELECT schemaname, tablename, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = ANY(:schemas)
    ORDER BY schemaname, tablename, indexname
    """
)

_ROW_COUNTS_SQL = text(
    """
    SELECT schemaname, relname, n_live_tup
    FROM pg_stat_user_tables
    WHERE schemaname = ANY(:schemas)
    """
)

_TABLE_COMMENTS_SQL = text(
    """
    SELECT n.nspname AS table_schema, c.relname AS table_name,
           obj_description(c.oid, 'pg_class') AS comment
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'v')
    """
)

_COLUMN_COMMENTS_SQL = text(
    """
    SELECT n.nspname AS table_schema, c.relname AS table_name, a.attname AS column_name,
           col_description(c.oid, a.attnum) AS comment
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid
    WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'v') AND a.attnum > 0
      AND NOT a.attisdropped
    """
)


class PostgresSchemaIntrospector:
    async def introspect(
        self, engine: AsyncEngine, *, schema_allowlist: list[str]
    ) -> SchemaIntrospectionResult:
        if not schema_allowlist:
            # Fail closed — see module docstring and query_guard's identical policy.
            return SchemaIntrospectionResult(schemas=[])

        params = {"schemas": schema_allowlist}
        async with engine.connect() as conn:
            tables_rows = (await conn.execute(_TABLES_SQL, params)).mappings().all()
            columns_rows = (await conn.execute(_COLUMNS_SQL, params)).mappings().all()
            pk_rows = (await conn.execute(_PRIMARY_KEYS_SQL, params)).mappings().all()
            fk_rows = (await conn.execute(_FOREIGN_KEYS_SQL, params)).mappings().all()
            unique_rows = (await conn.execute(_UNIQUE_CONSTRAINTS_SQL, params)).mappings().all()
            index_rows = (await conn.execute(_INDEXES_SQL, params)).mappings().all()
            row_count_rows = (await conn.execute(_ROW_COUNTS_SQL, params)).mappings().all()
            table_comment_rows = (await conn.execute(_TABLE_COMMENTS_SQL, params)).mappings().all()
            column_comment_rows = (
                (await conn.execute(_COLUMN_COMMENTS_SQL, params)).mappings().all()
            )

        table_comments = {
            (r["table_schema"], r["table_name"]): r["comment"]
            for r in table_comment_rows
            if r["comment"]
        }
        column_comments = {
            (r["table_schema"], r["table_name"], r["column_name"]): r["comment"]
            for r in column_comment_rows
            if r["comment"]
        }
        row_counts = {(r["schemaname"], r["relname"]): int(r["n_live_tup"]) for r in row_count_rows}

        tables: dict[tuple[str, str], TableMetadata] = {}
        for row in tables_rows:
            key = (row["table_schema"], row["table_name"])
            tables[key] = TableMetadata(
                schema_name=row["table_schema"],
                name=row["table_name"],
                kind="view" if row["table_type"] == "VIEW" else "table",
                comment=table_comments.get(key),
                approximate_row_count=row_counts.get(key),
            )

        for row in columns_rows:
            key = (row["table_schema"], row["table_name"])
            table = tables.get(key)
            if table is None:
                continue
            table.columns.append(
                ColumnMetadata(
                    name=row["column_name"],
                    sql_type=row["data_type"],
                    is_nullable=row["is_nullable"] == "YES",
                    ordinal_position=row["ordinal_position"],
                    comment=column_comments.get((*key, row["column_name"])),
                )
            )

        for row in pk_rows:
            key = (row["table_schema"], row["table_name"])
            table = tables.get(key)
            if table is not None:
                table.primary_key_columns.append(row["column_name"])

        fk_groups: dict[tuple[str, str, str], ForeignKeyMetadata] = {}
        for row in fk_rows:
            fk_key = (row["table_schema"], row["table_name"], row["constraint_name"])
            fk = fk_groups.get(fk_key)
            if fk is None:
                fk = ForeignKeyMetadata(
                    constraint_name=row["constraint_name"],
                    columns=[],
                    referenced_schema=row["ref_schema"],
                    referenced_table=row["ref_table"],
                    referenced_columns=[],
                )
                fk_groups[fk_key] = fk
                table = tables.get((row["table_schema"], row["table_name"]))
                if table is not None:
                    table.foreign_keys.append(fk)
            fk.columns.append(row["column_name"])
            fk.referenced_columns.append(row["ref_column"])

        unique_groups: dict[tuple[str, str, str], UniqueConstraintMetadata] = {}
        for row in unique_rows:
            uq_key = (row["table_schema"], row["table_name"], row["constraint_name"])
            uq = unique_groups.get(uq_key)
            if uq is None:
                uq = UniqueConstraintMetadata(constraint_name=row["constraint_name"], columns=[])
                unique_groups[uq_key] = uq
                table = tables.get((row["table_schema"], row["table_name"]))
                if table is not None:
                    table.unique_constraints.append(uq)
            uq.columns.append(row["column_name"])

        for row in index_rows:
            key = (row["schemaname"], row["tablename"])
            table = tables.get(key)
            if table is not None:
                table.indexes.append(
                    IndexMetadata(
                        name=row["indexname"],
                        columns=_extract_index_columns(row["indexdef"]),
                        is_unique="UNIQUE" in row["indexdef"].split("(")[0].upper(),
                    )
                )

        return SchemaIntrospectionResult(
            schemas=list(schema_allowlist),
            tables=sorted(tables.values(), key=lambda t: t.qualified_name),
        )


def _extract_index_columns(index_def: str) -> list[str]:
    """`indexdef` looks like `CREATE INDEX ix_x ON schema.table USING btree (col1, col2)` —
    this pulls out `col1, col2` without needing a full SQL parser."""

    if "(" not in index_def or ")" not in index_def:
        return []
    inner = index_def[index_def.index("(") + 1 : index_def.rindex(")")]
    return [part.strip().split(" ")[0] for part in inner.split(",") if part.strip()]
