"""Tests for the data-quality checks (Phase 9) — each catches a real, concrete defect shape."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from hermes_rpt.datasets.quality import (
    DataQualityReport,
    QualitySeverity,
    check_class_imbalance,
    check_cross_tenant_contamination,
    check_duplicate_ids,
    check_future_timestamps,
    check_invalid_date_ordering,
    check_missing_labels,
    check_negative_numeric,
    check_unrecognised_values,
)

_NOW = datetime(2026, 7, 27, tzinfo=UTC)


def test_check_duplicate_ids_flags_repeated_identity() -> None:
    rows = [{"id": "a"}, {"id": "b"}, {"id": "a"}]
    issue = check_duplicate_ids(rows, id_field="id", check="dup")
    assert issue is not None
    assert issue.severity == QualitySeverity.ERROR
    assert issue.count == 1
    assert issue.example_ids == ("a",)


def test_check_duplicate_ids_passes_with_unique_ids() -> None:
    rows = [{"id": "a"}, {"id": "b"}]
    assert check_duplicate_ids(rows, id_field="id", check="dup") is None


def test_check_invalid_date_ordering_flags_later_before_earlier() -> None:
    rows = [
        {"id": "t1", "planned": _NOW, "actual": _NOW.replace(hour=1)},  # fine: after
        {"id": "t2", "planned": _NOW, "actual": _NOW.replace(day=25)},  # bad: before
    ]
    issue = check_invalid_date_ordering(
        rows, id_field="id", earlier_field="planned", later_field="actual", check="dates"
    )
    assert issue is not None
    assert issue.count == 1
    assert issue.example_ids == ("t2",)


def test_check_invalid_date_ordering_ignores_rows_missing_either_timestamp() -> None:
    rows = [{"id": "t1", "planned": _NOW, "actual": None}]
    assert (
        check_invalid_date_ordering(
            rows, id_field="id", earlier_field="planned", later_field="actual", check="dates"
        )
        is None
    )


def test_check_negative_numeric_flags_negative_values() -> None:
    rows = [{"id": "a", "distance": 10.0}, {"id": "b", "distance": -5.0}]
    issue = check_negative_numeric(rows, id_field="id", field="distance", check="neg")
    assert issue is not None
    assert issue.example_ids == ("b",)


def test_check_unrecognised_values_flags_values_outside_allowed_set() -> None:
    rows = [{"id": "a", "status": "completed"}, {"id": "b", "status": "who_knows"}]
    issue = check_unrecognised_values(
        rows, id_field="id", field="status", allowed_values=frozenset({"completed"}), check="status"
    )
    assert issue is not None
    assert issue.severity == QualitySeverity.WARNING
    assert issue.example_ids == ("b",)


def test_check_future_timestamps_flags_values_after_now() -> None:
    rows = [{"id": "a", "ts": _NOW}, {"id": "b", "ts": _NOW.replace(year=2030)}]
    issue = check_future_timestamps(rows, id_field="id", field="ts", now=_NOW, check="future")
    assert issue is not None
    assert issue.example_ids == ("b",)


def test_check_missing_labels_counts_none_values() -> None:
    issue = check_missing_labels([1, 0, None, None])
    assert issue is not None
    assert issue.count == 2
    assert issue.severity == QualitySeverity.INFO


def test_check_missing_labels_passes_with_no_missing() -> None:
    assert check_missing_labels([1, 0, 1]) is None


def test_check_class_imbalance_flags_severe_skew() -> None:
    labels = [0] * 990 + [1] * 10  # 1% minority class
    issue = check_class_imbalance(labels, min_minority_fraction=0.02)
    assert issue is not None
    assert issue.severity == QualitySeverity.WARNING


def test_check_class_imbalance_passes_with_reasonable_balance() -> None:
    labels = [0] * 700 + [1] * 300
    assert check_class_imbalance(labels, min_minority_fraction=0.02) is None


def test_check_cross_tenant_contamination_flags_foreign_tenant_ids() -> None:
    expected = uuid.uuid4()
    foreign = uuid.uuid4()
    issue = check_cross_tenant_contamination(
        [expected, expected, foreign], expected_tenant_id=expected
    )
    assert issue is not None
    assert issue.severity == QualitySeverity.ERROR
    assert issue.count == 1


def test_check_cross_tenant_contamination_passes_when_all_match() -> None:
    expected = uuid.uuid4()
    assert (
        check_cross_tenant_contamination([expected, expected], expected_tenant_id=expected) is None
    )


def test_report_passed_is_false_with_any_error_severity_issue() -> None:
    report = DataQualityReport(
        row_count=10,
        issues=(
            check_negative_numeric([{"id": "a", "d": -1.0}], id_field="id", field="d", check="neg"),
        ),
    )
    assert report.passed is False


def test_report_passed_is_true_with_only_warning_or_info_issues() -> None:
    report = DataQualityReport(
        row_count=10,
        issues=(check_missing_labels([1, None]),),
    )
    assert report.passed is True


def test_report_passed_is_true_with_no_issues() -> None:
    report = DataQualityReport(row_count=10, issues=())
    assert report.passed is True
