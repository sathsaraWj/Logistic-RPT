"""Unit conversion layer tests (Phase 6)."""

from __future__ import annotations

import pytest

from hermes_rpt.ontology.units import (
    DistanceUnit,
    MassUnit,
    VolumeUnit,
    convert_distance,
    convert_mass,
    convert_volume,
)


def test_identity_conversion_returns_the_same_value() -> None:
    assert (
        convert_distance(100.0, from_unit=DistanceUnit.KILOMETRES, to_unit=DistanceUnit.KILOMETRES)
        == 100.0
    )


def test_miles_to_kilometres() -> None:
    result = convert_distance(1.0, from_unit=DistanceUnit.MILES, to_unit=DistanceUnit.KILOMETRES)
    assert result == pytest.approx(1.609344)


def test_kilometres_to_miles_round_trip() -> None:
    km = 250.0
    miles = convert_distance(km, from_unit=DistanceUnit.KILOMETRES, to_unit=DistanceUnit.MILES)
    back_to_km = convert_distance(
        miles, from_unit=DistanceUnit.MILES, to_unit=DistanceUnit.KILOMETRES
    )
    assert back_to_km == pytest.approx(km)


def test_us_gallons_to_litres() -> None:
    result = convert_volume(1.0, from_unit=VolumeUnit.US_GALLONS, to_unit=VolumeUnit.LITRES)
    assert result == pytest.approx(3.785411784)


def test_uk_gallons_differ_from_us_gallons() -> None:
    us = convert_volume(1.0, from_unit=VolumeUnit.US_GALLONS, to_unit=VolumeUnit.LITRES)
    uk = convert_volume(1.0, from_unit=VolumeUnit.UK_GALLONS, to_unit=VolumeUnit.LITRES)
    assert us != uk


def test_pounds_to_kilograms() -> None:
    result = convert_mass(1.0, from_unit=MassUnit.POUNDS, to_unit=MassUnit.KILOGRAMS)
    assert result == pytest.approx(0.45359237)


def test_unsupported_unit_raises_key_error() -> None:
    with pytest.raises(KeyError):
        convert_distance(1.0, from_unit="furlongs", to_unit=DistanceUnit.KILOMETRES)  # type: ignore[arg-type]
