"""Tests for dataset checksum determinism (Phase 9) — "dataset checksum" /
reproducibility requirement.
"""

from __future__ import annotations

from hermes_rpt.datasets.manifest import compute_dataset_checksum


def test_checksum_is_deterministic_for_identical_input() -> None:
    rows = [{"a": 1.0, "b": None}, {"a": 2.0, "b": True}]
    labels = [0, 1]
    assert compute_dataset_checksum(rows, labels=labels) == compute_dataset_checksum(
        rows, labels=labels
    )


def test_checksum_changes_when_a_feature_value_changes() -> None:
    rows_a = [{"a": 1.0}]
    rows_b = [{"a": 1.5}]
    assert compute_dataset_checksum(rows_a, labels=[0]) != compute_dataset_checksum(
        rows_b, labels=[0]
    )


def test_checksum_changes_when_a_label_changes() -> None:
    rows = [{"a": 1.0}]
    assert compute_dataset_checksum(rows, labels=[0]) != compute_dataset_checksum(rows, labels=[1])


def test_checksum_changes_when_row_order_changes() -> None:
    rows_forward = [{"a": 1.0}, {"a": 2.0}]
    rows_reversed = [{"a": 2.0}, {"a": 1.0}]
    labels = [0, 1]
    assert compute_dataset_checksum(rows_forward, labels=labels) != compute_dataset_checksum(
        rows_reversed, labels=labels
    )


def test_checksum_is_a_hex_sha256_digest() -> None:
    checksum = compute_dataset_checksum([{"a": 1.0}], labels=[0])
    assert len(checksum) == 64
    int(checksum, 16)  # raises ValueError if not valid hex
