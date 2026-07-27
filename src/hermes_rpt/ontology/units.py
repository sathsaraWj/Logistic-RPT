"""Unit conversion layer (Phase 6).

Internally, the ontology always stores distance in kilometres, volume in litres, and mass in
kilograms (Phase 6 examples). This module is what a schema mapping (Phase 7) calls when a
customer's source data is in a different unit — it never changes what the canonical field
means, only how a raw source value gets converted into it.
"""

from __future__ import annotations

from enum import StrEnum


class DistanceUnit(StrEnum):
    KILOMETRES = "kilometres"
    MILES = "miles"


class VolumeUnit(StrEnum):
    LITRES = "litres"
    US_GALLONS = "us_gallons"
    UK_GALLONS = "uk_gallons"


class MassUnit(StrEnum):
    KILOGRAMS = "kilograms"
    POUNDS = "pounds"


CANONICAL_DISTANCE_UNIT = DistanceUnit.KILOMETRES
CANONICAL_VOLUME_UNIT = VolumeUnit.LITRES
CANONICAL_MASS_UNIT = MassUnit.KILOGRAMS

_KM_PER_MILE = 1.609344
_LITRES_PER_US_GALLON = 3.785411784
_LITRES_PER_UK_GALLON = 4.54609
_KG_PER_POUND = 0.45359237

_TO_CANONICAL_DISTANCE: dict[DistanceUnit, float] = {
    DistanceUnit.KILOMETRES: 1.0,
    DistanceUnit.MILES: _KM_PER_MILE,
}
_TO_CANONICAL_VOLUME: dict[VolumeUnit, float] = {
    VolumeUnit.LITRES: 1.0,
    VolumeUnit.US_GALLONS: _LITRES_PER_US_GALLON,
    VolumeUnit.UK_GALLONS: _LITRES_PER_UK_GALLON,
}
_TO_CANONICAL_MASS: dict[MassUnit, float] = {
    MassUnit.KILOGRAMS: 1.0,
    MassUnit.POUNDS: _KG_PER_POUND,
}


def convert_distance(value: float, *, from_unit: DistanceUnit, to_unit: DistanceUnit) -> float:
    canonical_value = value * _TO_CANONICAL_DISTANCE[from_unit]
    return canonical_value / _TO_CANONICAL_DISTANCE[to_unit]


def convert_volume(value: float, *, from_unit: VolumeUnit, to_unit: VolumeUnit) -> float:
    canonical_value = value * _TO_CANONICAL_VOLUME[from_unit]
    return canonical_value / _TO_CANONICAL_VOLUME[to_unit]


def convert_mass(value: float, *, from_unit: MassUnit, to_unit: MassUnit) -> float:
    canonical_value = value * _TO_CANONICAL_MASS[from_unit]
    return canonical_value / _TO_CANONICAL_MASS[to_unit]


class CanonicalUnit(StrEnum):
    """The unit vocabulary usable on an ontology FLOAT field (hermes_rpt.ontology.schema).
    Every member here is already a *canonical* unit (the one values are stored in
    internally) — source-side units (miles, gallons, pounds, ...) are a mapping-layer (Phase 7)
    concept, not an ontology-field concept, which is why this enum is deliberately smaller than
    DistanceUnit/VolumeUnit/MassUnit combined."""

    KILOMETRES = "kilometres"
    LITRES = "litres"
    KILOGRAMS = "kilograms"


def convert_to_canonical(value: float, *, source_unit: str, canonical_unit: CanonicalUnit) -> float:
    """Used by schema mapping (Phase 7) to convert a source-side value expressed in
    `source_unit` into the ontology field's canonical unit. Raises `ValueError` if
    `source_unit` isn't a recognised unit in the same family as `canonical_unit` (e.g. asking
    to convert "pounds" into a kilometres field) — mapping validation calls this eagerly so
    that mistake is caught at mapping-approval time, not silently at extraction time."""

    if canonical_unit == CanonicalUnit.KILOMETRES:
        try:
            distance_unit = DistanceUnit(source_unit)
        except ValueError as exc:
            raise _unsupported_unit_error(source_unit, canonical_unit, DistanceUnit) from exc
        return convert_distance(value, from_unit=distance_unit, to_unit=DistanceUnit.KILOMETRES)

    if canonical_unit == CanonicalUnit.LITRES:
        try:
            volume_unit = VolumeUnit(source_unit)
        except ValueError as exc:
            raise _unsupported_unit_error(source_unit, canonical_unit, VolumeUnit) from exc
        return convert_volume(value, from_unit=volume_unit, to_unit=VolumeUnit.LITRES)

    try:
        mass_unit = MassUnit(source_unit)
    except ValueError as exc:
        raise _unsupported_unit_error(source_unit, canonical_unit, MassUnit) from exc
    return convert_mass(value, from_unit=mass_unit, to_unit=MassUnit.KILOGRAMS)


def _unsupported_unit_error(
    source_unit: str, canonical_unit: CanonicalUnit, family: type[StrEnum]
) -> ValueError:
    valid = [member.value for member in family]
    return ValueError(
        f"{source_unit!r} is not a valid unit for {canonical_unit.value!r} "
        f"(expected one of {valid})"
    )
