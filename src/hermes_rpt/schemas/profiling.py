"""Optional, limited data profiling (Phase 5).

Off by default. When enabled, profiling computes small, aggregate summaries only — never
complete source rows — and never touches a column that looks like a credential (denylisted by
name pattern) or is likely personal (masked: aggregate stats only, no sample values). Every
profiling run is audited by the caller (`hermes_rpt.schemas.service`).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes_rpt.schemas.introspection import TableMetadata

_DEFAULT_MAX_SAMPLE_ROWS = 200
_DEFAULT_MAX_TABLES = 20

# Never profiled at all — not even aggregate stats — regardless of denylist configuration.
_CREDENTIAL_COLUMN_PATTERN = re.compile(
    r"(password|secret|token|credential|api[_-]?key|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

# Profiled, but only as aggregate stats — sample values are never included for these.
_DEFAULT_PERSONAL_FIELD_PATTERNS = (
    re.compile(r"(email|e[-_]?mail)", re.IGNORECASE),
    re.compile(r"(phone|mobile|telephone)", re.IGNORECASE),
    re.compile(r"(ssn|social[_-]?security|national[_-]?id|passport)", re.IGNORECASE),
    re.compile(r"(address|street|zip|postal)", re.IGNORECASE),
    re.compile(r"(first[_-]?name|last[_-]?name|full[_-]?name|surname)", re.IGNORECASE),
    re.compile(r"(date[_-]?of[_-]?birth|dob|birthdate)", re.IGNORECASE),
)


class ProfilingConfig(BaseModel):
    enabled: bool = False
    sample_rows: int = Field(default=100, le=_DEFAULT_MAX_SAMPLE_ROWS, gt=0)
    max_tables: int = Field(default=10, le=_DEFAULT_MAX_TABLES, gt=0)
    denylisted_column_patterns: list[str] = Field(default_factory=list)


class ColumnProfileSummary(BaseModel):
    column_name: str
    is_credential_excluded: bool = False
    is_masked_personal_field: bool = False
    null_fraction: float | None = None
    distinct_count_estimate: int | None = None
    sample_values: list[str] | None = None  # only ever populated for non-masked columns


class TableProfileSummary(BaseModel):
    table: str
    sampled_row_count: int
    columns: list[ColumnProfileSummary] = Field(default_factory=list)


def is_credential_column(name: str, extra_denylist: list[str]) -> bool:
    if _CREDENTIAL_COLUMN_PATTERN.search(name):
        return True
    return any(re.search(pattern, name, re.IGNORECASE) for pattern in extra_denylist)


def is_likely_personal_field(name: str) -> bool:
    return any(pattern.search(name) for pattern in _DEFAULT_PERSONAL_FIELD_PATTERNS)


def _assert_safe_identifier(identifier: str) -> None:
    """`table.schema_name`/`table.name` come from our own introspection (Phase 5), not client
    input — but the profiling query still can't use a bind parameter for an identifier, so this
    is defense in depth against a pathological/compromised identifier before it's interpolated
    into SQL text, rather than trusting the source unconditionally."""

    if '"' in identifier or "\x00" in identifier:
        raise ValueError(f"Unsafe identifier rejected: {identifier!r}")


class TableProfiler:
    async def profile_table(
        self, engine: AsyncEngine, table: TableMetadata, config: ProfilingConfig
    ) -> TableProfileSummary:
        _assert_safe_identifier(table.schema_name)
        _assert_safe_identifier(table.name)

        async with engine.connect() as conn:
            # Table/schema identifiers can't be bind parameters; `_assert_safe_identifier`
            # above is the mitigation bandit's B608 heuristic can't see statically.
            sample_result = await conn.execute(
                text(
                    f'SELECT * FROM "{table.schema_name}"."{table.name}" '  # noqa: S608 # nosec B608
                    f"LIMIT :limit"
                ),
                {"limit": config.sample_rows},
            )
            rows = sample_result.mappings().all()

        summaries: list[ColumnProfileSummary] = []
        for column in table.columns:
            if is_credential_column(column.name, config.denylisted_column_patterns):
                summaries.append(
                    ColumnProfileSummary(column_name=column.name, is_credential_excluded=True)
                )
                continue

            values = [row[column.name] for row in rows]
            non_null = [v for v in values if v is not None]
            null_fraction = 1.0 - (len(non_null) / len(values)) if values else None
            distinct_estimate = len({str(v) for v in non_null}) if non_null else 0
            masked = is_likely_personal_field(column.name)

            summaries.append(
                ColumnProfileSummary(
                    column_name=column.name,
                    is_masked_personal_field=masked,
                    null_fraction=null_fraction,
                    distinct_count_estimate=distinct_estimate,
                    sample_values=None if masked else [str(v) for v in non_null[:5]],
                )
            )

        return TableProfileSummary(
            table=table.qualified_name, sampled_row_count=len(rows), columns=summaries
        )
