"""Data-quality checks (Phase 9). Each check is a small, pure function operating on already-
fetched rows/labels — nothing here touches a database or a secret. `DataQualityReport` is safe
to persist in a dataset manifest: it carries counts and business identifiers only, never a raw
customer value ("manifests may contain metadata but not sensitive raw values").
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

_MAX_EXAMPLE_IDS = 5
_MIN_MINORITY_CLASS_FRACTION = 0.02


class QualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QualityIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    severity: QualitySeverity
    count: int
    message: str
    example_ids: tuple[str, ...] = ()  # business identifiers only — never a raw field value


class DataQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_count: int
    issues: tuple[QualityIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity == QualitySeverity.ERROR for issue in self.issues)


def check_duplicate_ids(
    rows: list[dict[str, Any]], *, id_field: str, check: str
) -> QualityIssue | None:
    counts = Counter(row[id_field] for row in rows if row.get(id_field) is not None)
    duplicates = [str(value) for value, n in counts.items() if n > 1]
    if not duplicates:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.ERROR,
        count=len(duplicates),
        message=f"{len(duplicates)} duplicate value(s) found for {id_field!r}",
        example_ids=tuple(duplicates[:_MAX_EXAMPLE_IDS]),
    )


def check_invalid_date_ordering(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    earlier_field: str,
    later_field: str,
    check: str,
) -> QualityIssue | None:
    """Flags any row where `later_field` is populated but strictly before `earlier_field` —
    e.g. a trip's `actual_arrival_at` before its own `planned_departure_at`."""

    bad_ids: list[str] = []
    for row in rows:
        earlier = row.get(earlier_field)
        later = row.get(later_field)
        if isinstance(earlier, datetime) and isinstance(later, datetime) and later < earlier:
            bad_ids.append(str(row.get(id_field)))
    if not bad_ids:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.ERROR,
        count=len(bad_ids),
        message=f"{len(bad_ids)} row(s) have {later_field!r} before {earlier_field!r}",
        example_ids=tuple(bad_ids[:_MAX_EXAMPLE_IDS]),
    )


def check_negative_numeric(
    rows: list[dict[str, Any]], *, id_field: str, field: str, check: str
) -> QualityIssue | None:
    bad_ids: list[str] = []
    for row in rows:
        value = row.get(field)
        if isinstance(value, int | float) and value < 0:
            bad_ids.append(str(row.get(id_field)))
    if not bad_ids:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.ERROR,
        count=len(bad_ids),
        message=f"{len(bad_ids)} row(s) have a negative {field!r}",
        example_ids=tuple(bad_ids[:_MAX_EXAMPLE_IDS]),
    )


def check_unrecognised_values(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    field: str,
    allowed_values: frozenset[str],
    check: str,
) -> QualityIssue | None:
    bad_ids: list[str] = []
    for row in rows:
        value = row.get(field)
        if value is not None and value not in allowed_values:
            bad_ids.append(str(row.get(id_field)))
    if not bad_ids:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.WARNING,
        count=len(bad_ids),
        message=f"{len(bad_ids)} row(s) have an unrecognised {field!r}",
        example_ids=tuple(bad_ids[:_MAX_EXAMPLE_IDS]),
    )


def check_future_timestamps(
    rows: list[dict[str, Any]], *, id_field: str, field: str, now: datetime, check: str
) -> QualityIssue | None:
    bad_ids: list[str] = []
    for row in rows:
        value = row.get(field)
        if isinstance(value, datetime) and value > now:
            bad_ids.append(str(row.get(id_field)))
    if not bad_ids:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.ERROR,
        count=len(bad_ids),
        message=f"{len(bad_ids)} row(s) have a {field!r} in the future",
        example_ids=tuple(bad_ids[:_MAX_EXAMPLE_IDS]),
    )


def check_missing_labels(
    labels: list[int | None], *, check: str = "missing_labels"
) -> QualityIssue | None:
    missing = sum(1 for label in labels if label is None)
    if missing == 0:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.INFO,
        count=missing,
        message=(
            f"{missing} of {len(labels)} candidate row(s) have no label yet (outcome not known "
            "as of the dataset's time range) and were excluded"
        ),
    )


def check_class_imbalance(
    labels: list[int],
    *,
    min_minority_fraction: float = _MIN_MINORITY_CLASS_FRACTION,
    check: str = "class_imbalance",
) -> QualityIssue | None:
    if not labels:
        return None
    positive_fraction = sum(labels) / len(labels)
    minority_fraction = min(positive_fraction, 1 - positive_fraction)
    if minority_fraction >= min_minority_fraction:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.WARNING,
        count=len(labels),
        message=(
            f"Minority class fraction is {minority_fraction:.4f}, below the "
            f"{min_minority_fraction:.4f} threshold — severe class imbalance"
        ),
    )


def check_cross_tenant_contamination(
    tenant_ids: list[uuid.UUID],
    *,
    expected_tenant_id: uuid.UUID,
    check: str = "cross_tenant_contamination",
) -> QualityIssue | None:
    """A dataset cannot contain multiple tenants by accident — this is a structural invariant
    check on the *assembled* dataset (every row's tenant_id is attached by the builder itself
    from a verified `TenantContext`, never read off a customer table), not a check on raw
    customer data."""

    foreign = [str(tid) for tid in tenant_ids if tid != expected_tenant_id]
    if not foreign:
        return None
    return QualityIssue(
        check=check,
        severity=QualitySeverity.ERROR,
        count=len(foreign),
        message=f"{len(foreign)} row(s) belong to a tenant other than {expected_tenant_id}",
    )
