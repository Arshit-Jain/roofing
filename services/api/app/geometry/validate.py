"""Evidence-based geometry checks with numeric outcomes and warning-level tolerances."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.geometry.topology import polygon_area

CheckStatus = Literal["pass", "warn", "fail", "not_evaluable"]


def check_value(
    name: str,
    actual: float | None,
    expected: float | None,
    tolerance_percent: float,
    *,
    unit: str,
    one_sided: bool = False,
) -> dict[str, Any]:
    if actual is None or expected is None:
        return {
            "name": name,
            "status": "not_evaluable",
            "actual": actual,
            "expected": expected,
            "tolerancePercent": tolerance_percent,
            "unit": unit,
            "differencePercent": None,
            "oneSided": one_sided,
        }
    if expected == 0:
        difference = 0.0 if actual == 0 else math.inf
    else:
        difference = abs(actual - expected) / abs(expected) * 100
    if one_sided:
        # Roofr reports these square-foot labels rounded upward to whole units.
        # Permit the display rounding unit below the label and the configured
        # scale tolerance, while still rejecting a substantially undersized face.
        rounding_unit = 1.0 if unit == "sq ft" else 0.0
        passes = (
            actual >= expected - rounding_unit - abs(expected) * tolerance_percent / 100
            and actual <= expected + abs(expected) * tolerance_percent / 100
        )
    else:
        passes = difference <= tolerance_percent
    return {
        "name": name,
        "status": "pass" if passes else "warn",
        "actual": round(actual, 6),
        "expected": round(expected, 6),
        "tolerancePercent": tolerance_percent,
        "unit": unit,
        "differencePercent": round(difference, 3) if math.isfinite(difference) else None,
        "oneSided": one_sided,
    }


def validate_facet_areas(
    facets: Sequence[Mapping[str, Any]],
    *,
    feet_per_point: float | None,
    tolerance_percent: float = 5.0,
    ignore_unlabeled_facet_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ignored_ids = ignore_unlabeled_facet_ids or set()
    for facet in facets:
        area_label = facet.get("areaLabelSqFt")
        polygon = facet.get("polygonPagePoints")
        page_scale = facet.get("feetPerPoint", feet_per_point)
        if area_label is None and str(facet.get("id", "")) in ignored_ids:
            # A reviewer-created outline has no source area label to compare;
            # its area is still calculated from its calibrated geometry.
            continue
        if (
            not isinstance(area_label, (int, float))
            or not isinstance(polygon, Sequence)
            or len(polygon) < 3
            or not isinstance(page_scale, (int, float))
        ):
            checks.append(
                check_value(
                    f"facet:{facet.get('id', 'unknown')}:slope_area",
                    None,
                    float(area_label) if isinstance(area_label, (int, float)) else None,
                    tolerance_percent,
                    unit="sq ft",
                )
            )
            continue
        plan_area_points = polygon_area([(float(point[0]), float(point[1])) for point in polygon])
        plan_area = plan_area_points * float(page_scale) ** 2
        pitch = facet.get("pitch")
        slope_area = plan_area
        if isinstance(pitch, Mapping):
            run = float(pitch.get("run", 0))
            rise = float(pitch.get("rise", 0))
            if run <= 0 or rise < 0:
                slope_area = math.nan
            else:
                slope_area *= math.sqrt(1 + (rise / run) ** 2)
        checks.append(
            check_value(
                f"facet:{facet.get('id', 'unknown')}:slope_area",
                slope_area if math.isfinite(slope_area) else None,
                float(area_label),
                tolerance_percent,
                unit="sq ft",
                one_sided=bool(facet.get("areaRoundedUp", False)),
            )
        )
    return checks


def validate_class_totals(
    drawn_lengths: Mapping[str, float],
    report_lengths: Mapping[str, float],
    *,
    tolerance_percent: float = 3.0,
) -> list[dict[str, Any]]:
    keys = sorted(set(drawn_lengths) | set(report_lengths))
    checks = []
    for edge_class in keys:
        expected = report_lengths.get(edge_class)
        actual = drawn_lengths.get(edge_class)
        # A missing class is evidence of zero only when the report says zero.
        # Missing positive classes remain unevaluable.
        if actual is None and expected == 0:
            actual = 0.0
        checks.append(
            check_value(
                f"edge_class:{edge_class}",
                actual,
                expected,
                tolerance_percent,
                unit="ft",
                one_sided=False,
            )
        )
    return checks


def validate_geometry_counts(
    *,
    recovered_facets: int,
    expected_facets: int | None,
) -> dict[str, Any]:
    if expected_facets is None:
        return {
            "name": "facet_count",
            "status": "not_evaluable",
            "actual": recovered_facets,
            "expected": None,
            "tolerance": 0,
            "unit": "facets",
        }
    return {
        "name": "facet_count",
        "status": "pass" if recovered_facets == expected_facets else "warn",
        "actual": recovered_facets,
        "expected": expected_facets,
        "tolerance": 0,
        "unit": "facets",
    }


def validate_uniform_pitch_hypothesis(
    *,
    net_area_sq_ft: float,
    projected_area_sq_ft: float,
    predominant_pitch_rise: float,
    predominant_pitch_run: float,
    reported_edge_lengths: Mapping[str, float],
    calculated_edge_lengths: Mapping[str, float] | None,
    area_tolerance_percent: float = 5.0,
    edge_tolerance_percent: float = 3.0,
) -> dict[str, Any]:
    factor = math.sqrt(1 + (predominant_pitch_rise / predominant_pitch_run) ** 2)
    area_check = check_value(
        "uniform_pitch_sloped_area",
        projected_area_sq_ft * factor,
        net_area_sq_ft,
        area_tolerance_percent,
        unit="sq ft",
    )
    edge_checks = (
        validate_class_totals(
            calculated_edge_lengths,
            reported_edge_lengths,
            tolerance_percent=edge_tolerance_percent,
        )
        if calculated_edge_lengths is not None
        else [
            check_value(
                f"edge_class:{name}",
                None,
                expected,
                edge_tolerance_percent,
                unit="ft",
            )
            for name, expected in sorted(reported_edge_lengths.items())
        ]
    )
    return {
        "hypothesis": "assumedUniformPitch",
        "pitch": f"{predominant_pitch_rise:g}:{predominant_pitch_run:g}",
        "slopeFactor": round(factor, 6),
        "areaCheck": area_check,
        "edgeChecks": edge_checks,
        "status": "needs_review",
        "assumption": (
            "The predominant pitch is applied to each plane for this check only. "
            "The report does not provide per-plane pitch or downslope directions."
        ),
    }


def validate_pitch_gradients(
    geometry: Mapping[str, Any], *, tolerance_degrees: float = 1.0
) -> list[dict[str, Any]]:
    """Compare solved mesh gradients with each plane's reported pitch label."""
    solve = geometry.get("heightSolve")
    solved = isinstance(solve, Mapping) and solve.get("status") == "solved"
    vertices_by_id = {
        str(vertex.get("id")): vertex.get("plan")
        for vertex in geometry.get("vertices", [])
        if isinstance(vertex, Mapping)
    }
    checks: list[dict[str, Any]] = []
    for plane in geometry.get("planes", []):
        if not isinstance(plane, Mapping):
            continue
        pitch = plane.get("pitch")
        if not isinstance(pitch, Mapping):
            continue
        rise = _optional_number(pitch.get("rise"))
        run = _optional_number(pitch.get("run"))
        expected = math.degrees(math.atan2(rise, run)) if rise is not None and run else None
        actual = (
            _gradient_pitch_degrees(plane, vertices_by_id)
            if solved and expected is not None
            else None
        )
        if actual is None or expected is None:
            checks.append(
                {
                    "name": f"pitch_gradient:{plane.get('id', 'unknown')}",
                    "status": "not_evaluable",
                    "actual": None,
                    "expected": round(expected, 6) if expected is not None else None,
                    "tolerancePercent": None,
                    "toleranceDegrees": tolerance_degrees,
                    "unit": "degrees",
                    "reason": "The height solver did not produce a complete plane surface.",
                }
            )
            continue
        difference = abs(actual - expected)
        checks.append(
            {
                "name": f"pitch_gradient:{plane.get('id', 'unknown')}",
                "status": "pass" if difference <= tolerance_degrees else "warn",
                "actual": round(actual, 6),
                "expected": round(expected, 6),
                "differenceDegrees": round(difference, 6),
                "toleranceDegrees": tolerance_degrees,
                "unit": "degrees",
            }
        )
    return checks


def _gradient_pitch_degrees(
    plane: Mapping[str, Any], vertices_by_id: Mapping[str, Any]
) -> float | None:
    points: list[tuple[float, float, float]] = []
    for vertex_id in plane.get("boundaryVertexIds", []):
        point = vertices_by_id.get(str(vertex_id))
        if not isinstance(point, Sequence) or len(point) < 3:
            return None
        u = _optional_number(point[0])
        v = _optional_number(point[1])
        height = _optional_number(point[2])
        if u is None or v is None or height is None:
            return None
        points.append((u, v, height))
    if len(points) < 3:
        return None
    mean_u = sum(point[0] for point in points) / len(points)
    mean_v = sum(point[1] for point in points) / len(points)
    mean_h = sum(point[2] for point in points) / len(points)
    sum_uu = sum((u - mean_u) ** 2 for u, _, _ in points)
    sum_vv = sum((v - mean_v) ** 2 for _, v, _ in points)
    sum_uv = sum((u - mean_u) * (v - mean_v) for u, v, _ in points)
    sum_uh = sum((u - mean_u) * (h - mean_h) for u, _, h in points)
    sum_vh = sum((v - mean_v) * (h - mean_h) for _, v, h in points)
    determinant = sum_uu * sum_vv - sum_uv**2
    if abs(determinant) <= 1e-12:
        return None
    gradient_u = (sum_uh * sum_vv - sum_vh * sum_uv) / determinant
    gradient_v = (sum_vh * sum_uu - sum_uh * sum_uv) / determinant
    return math.degrees(math.atan(math.hypot(gradient_u, gradient_v)))


def validate_review_geometry(
    analysis: Mapping[str, Any],
    facets: Sequence[Mapping[str, Any]],
    geometry: Mapping[str, Any] | None,
    *,
    feet_per_point: float | None,
    scale_tolerance_percent: float,
    area_tolerance_percent: float,
    edge_tolerance_percent: float,
    pitch_tolerance_degrees: float = 1.0,
    manual_scale_confirmed: bool = False,
    topology_edited: bool = False,
    user_added_facet_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Recompute geometry checks after every review save from the edited evidence."""
    checks: list[dict[str, Any]] = []
    validation = analysis.get("validation")
    original_checks = validation.get("checks", []) if isinstance(validation, Mapping) else []
    for check in original_checks:
        if not isinstance(check, Mapping):
            continue
        name = str(check.get("name", ""))
        if name.startswith(("facet:", "edge_class:", "pitch_gradient:", "geometry_")) or name in {
            "facet_count",
            "3d_geometry_candidate",
            "manual_dimension_scale",
        }:
            continue
        if name in {"dimension_scale", "page_registration"} or name.startswith(
            "page_registration_"
        ):
            if not manual_scale_confirmed:
                checks.append(dict(check))
        else:
            # Preserve source-only aggregate hypotheses, but never retain checks
            # whose values depend on the geometry being edited.
            checks.append(dict(check))
    if manual_scale_confirmed and feet_per_point is not None:
        checks.append(
            {
                "name": "manual_dimension_scale",
                "status": "pass",
                "actual": round(feet_per_point, 8),
                "expected": None,
                "tolerancePercent": scale_tolerance_percent,
                "unit": "ft/pt",
                "reason": "Contractor confirmed this scale from a known report dimension.",
            }
        )
    checks.extend(
        validate_facet_areas(
            facets,
            feet_per_point=feet_per_point,
            tolerance_percent=area_tolerance_percent,
            ignore_unlabeled_facet_ids=user_added_facet_ids,
        )
    )
    expected_facets = analysis.get("expectedFacetCount")
    if isinstance(expected_facets, (int, float)) and not topology_edited:
        checks.append(
            validate_geometry_counts(
                recovered_facets=len(facets), expected_facets=int(expected_facets)
            )
        )

    if geometry is None:
        checks.append(
            check_value(
                "3d_geometry_candidate",
                None,
                1,
                0,
                unit="candidate",
            )
        )
    else:
        checks.extend(validate_pitch_gradients(geometry, tolerance_degrees=pitch_tolerance_degrees))
        planes_value = geometry.get("planes", [])
        planes = [plane for plane in planes_value if isinstance(plane, Mapping)]
        summary = analysis.get("reported", {})
        reported = summary if isinstance(summary, Mapping) else {}
        source_format = str(analysis.get("sourceFormat", ""))
        total_expected = _optional_number(
            reported.get("totalRoofAreaSqFt")
            if source_format == "roof_manager"
            else reported.get("totalRoofAreaSqFt")
        )
        projected_expected = _optional_number(reported.get("projectedAreaSqFt"))
        if projected_expected is not None:
            checks.append(
                check_value(
                    "geometry_projected_area",
                    sum(_optional_number(plane.get("planAreaSqFt")) or 0 for plane in planes),
                    projected_expected,
                    area_tolerance_percent,
                    unit="sq ft",
                )
            )
        surface_actual = sum(
            _optional_number(plane.get("meshSurfaceAreaSqFt")) or 0 for plane in planes
        )
        if total_expected is not None:
            checks.append(
                check_value(
                    "geometry_sloped_area",
                    surface_actual,
                    total_expected,
                    area_tolerance_percent,
                    unit="sq ft",
                )
            )
        system_areas = {
            "pitched": _optional_number(reported.get("pitchedRoofAreaSqFt")),
            "flat": _optional_number(reported.get("flatRoofAreaSqFt")),
        }
        for roof_type, expected in system_areas.items():
            if expected is not None:
                checks.append(
                    check_value(
                        f"geometry_{roof_type}_area",
                        sum(
                            _optional_number(plane.get("meshSurfaceAreaSqFt")) or 0
                            for plane in planes
                            if plane.get("roofType") == roof_type
                        ),
                        expected,
                        area_tolerance_percent,
                        unit="sq ft",
                    )
                )
        edge_expected = _reported_edge_totals(analysis)
        if edge_expected:
            height_solve = geometry.get("heightSolve")
            lengths = (
                _geometry_edge_lengths(geometry)
                if isinstance(height_solve, Mapping) and height_solve.get("status") == "solved"
                else None
            )
            if lengths is None:
                checks.extend(
                    check_value(
                        f"edge_class:{edge_class}",
                        None,
                        expected,
                        edge_tolerance_percent,
                        unit="ft",
                    )
                    for edge_class, expected in sorted(edge_expected.items())
                )
            else:
                for edge_class, expected in edge_expected.items():
                    actual = lengths.get(edge_class)
                    if actual is None and expected == 0:
                        actual = 0.0
                    checks.append(
                        check_value(
                            f"edge_class:{edge_class}",
                            actual,
                            expected,
                            edge_tolerance_percent,
                            unit="ft",
                        )
                    )
    return checks


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _reported_edge_totals(analysis: Mapping[str, Any]) -> dict[str, float]:
    totals = analysis.get("edgeClassTotals")
    if isinstance(totals, Mapping) and isinstance(totals.get("reportedFeet"), Mapping):
        selected = totals["reportedFeet"]
    else:
        reported = analysis.get("reported")
        selected = reported.get("edgeTotalsFt") if isinstance(reported, Mapping) else None
    if not isinstance(selected, Mapping):
        return {}
    return {
        str(name): float(value)
        for name, value in selected.items()
        if _optional_number(value) is not None
    }


def _geometry_edge_lengths(geometry: Mapping[str, Any]) -> dict[str, float]:
    vertices = {
        str(vertex.get("id")): vertex.get("plan")
        for vertex in geometry.get("vertices", [])
        if isinstance(vertex, Mapping)
    }
    totals: dict[str, float] = {}
    for edge in geometry.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        endpoints = edge.get("vertexIds")
        if not isinstance(endpoints, Sequence) or len(endpoints) != 2:
            continue
        first = vertices.get(str(endpoints[0]))
        second = vertices.get(str(endpoints[1]))
        if (
            not isinstance(first, Sequence)
            or not isinstance(second, Sequence)
            or len(first) < 3
            or len(second) < 3
        ):
            continue
        length = math.dist(
            tuple(float(value) for value in first[:3]),
            tuple(float(value) for value in second[:3]),
        )
        edge_class = str(edge.get("type", "other"))
        totals[edge_class] = totals.get(edge_class, 0.0) + length
    return totals


def summarize_checks(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = [str(check.get("status")) for check in checks]
    return {
        "passCount": statuses.count("pass"),
        "warningCount": statuses.count("warn"),
        "failureCount": statuses.count("fail"),
        "notEvaluableCount": statuses.count("not_evaluable"),
        "status": "needs_review"
        if any(status in {"warn", "fail", "not_evaluable"} for status in statuses)
        else "passed",
    }
