"""Conservative adapter for Roof Manager's vector Roof Area Analysis pages.

Roof Manager exposes an overall traced outline and aggregate edge/area totals, but
does not report a pitch or downslope direction for each of its 31 planes. The adapter
therefore records vector evidence and tests the published aggregate pitch hypothesis;
it never promotes an outline into solved customer geometry without those constraints.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

from app.geometry.calibrate import (
    CalibrationSegment,
    DimensionObservation,
    fit_both_scale_hypotheses,
)
from app.geometry.diagram_extract import VectorPage, diagram_segments, numeric_words
from app.geometry.topology import polygonize_segments, segment_distance
from app.geometry.validate import (
    summarize_checks,
    validate_class_totals,
    validate_geometry_counts,
    validate_uniform_pitch_hypothesis,
)


def extract_roof_manager(
    pages: Sequence[VectorPage], *, settings: dict[str, float]
) -> dict[str, Any]:
    report_text = "\n".join(page.text for page in pages)
    drawing_page = next(
        (
            page
            for page in pages
            if "roof area analysis" in page.text.lower()
            and "areas by section" not in page.text.lower()
        ),
        None,
    )
    area_page = next((page for page in pages if "areas by section" in page.text.lower()), None)
    if drawing_page is None or area_page is None:
        return {
            "status": "unsupported",
            "reason": (
                "Roof Manager vector analysis pages could not be identified from their text labels."
            ),
            "facets": [],
            "warnings": [],
            "diagnostics": {"availablePageTitles": [page.title for page in pages]},
        }

    if not drawing_page.vector_supported or not area_page.vector_supported:
        pages_without_vectors = [
            page.page_number for page in (drawing_page, area_page) if not page.vector_supported
        ]
        return {
            "status": "unsupported",
            "reason": f"Roof Manager drawing page(s) {pages_without_vectors} are raster-only.",
            "facets": [],
            "warnings": [],
            "diagnostics": {"rasterOnlyPages": pages_without_vectors},
        }

    diagram = diagram_segments(
        drawing_page,
        min_y_fraction=0.08,
        max_y_fraction=0.68,
        min_x_fraction=0.05,
        max_x_fraction=0.95,
        require_legend_class=True,
        minimum_length=2.0,
    )
    polygons, polygon_diagnostics = polygonize_segments(
        (segment.segment for segment in diagram),
        snap_tolerance=max(settings["snapTolerancePoints"], 1.5),
        minimum_area=12.0,
    )

    total_area = _number_near(report_text, r"Total Roof Area\s*([\d,]+)\s*SF")
    footprint_area = _number_near(report_text, r"Footprint \(Projected\) Area\s*([\d,]+)\s*SF")
    predominant_pitch = _pitch_near(report_text)
    roof_planes = _number_near(report_text, r"ROOF PLANES\s*(\d+)")
    pitch_factor = (
        math.sqrt(1 + (predominant_pitch[0] / predominant_pitch[1]) ** 2)
        if predominant_pitch
        else 1.0
    )
    calibration_segments = [
        CalibrationSegment(
            id=f"p{drawing_page.page_number}-d{segment.drawing_index}-i{segment.item_index}-{index}",
            start=segment.start,
            end=segment.end,
            page_number=drawing_page.page_number,
            edge_class=segment.edge_class,
            sloped_factor=pitch_factor if segment.edge_class in {"hip", "valley"} else 1.0,
        )
        for index, segment in enumerate(diagram)
        if segment.edge_class is not None
    ]
    dimension_labels = _dimension_observations(drawing_page, calibration_segments)
    plan_fit, sloped_fit, chosen_hypothesis = fit_both_scale_hypotheses(
        dimension_labels,
        calibration_segments,
        max_label_distance_points=22.0,
        minimum_inliers=4,
    )
    selected_fit = (
        plan_fit
        if chosen_hypothesis == "plan"
        else sloped_fit
        if chosen_hypothesis == "sloped"
        else None
    )
    report_edge_lengths = _edge_totals(report_text)
    plan_edge_totals = _combine_lower_eave(
        _drawn_class_totals(calibration_segments, plan_fit.feet_per_point, "plan")
    )
    sloped_edge_totals = _combine_lower_eave(
        _drawn_class_totals(calibration_segments, sloped_fit.feet_per_point, "sloped")
    )
    drawn_edge_totals = plan_edge_totals if chosen_hypothesis == "plan" else sloped_edge_totals
    edge_checks = validate_class_totals(
        drawn_edge_totals,
        report_edge_lengths,
        tolerance_percent=settings["classTotalTolerancePercent"],
    )
    scale_check = _scale_check(selected_fit, settings["scaleTolerancePercent"])
    reported_sloped_area = total_area
    reported_projection = footprint_area

    uniform_pitch = (
        validate_uniform_pitch_hypothesis(
            net_area_sq_ft=reported_sloped_area,
            projected_area_sq_ft=reported_projection,
            predominant_pitch_rise=predominant_pitch[0],
            predominant_pitch_run=predominant_pitch[1],
            reported_edge_lengths={},
            calculated_edge_lengths=None,
            area_tolerance_percent=settings["facetAreaTolerancePercent"],
            edge_tolerance_percent=settings["classTotalTolerancePercent"],
        )
        if reported_projection is not None
        and reported_sloped_area is not None
        and predominant_pitch is not None
        else None
    )
    aggregate_check = (
        uniform_pitch["areaCheck"]
        if uniform_pitch
        else {
            "name": "aggregate_uniform_pitch_area_hypothesis",
            "status": "not_evaluable",
            "actual": None,
            "expected": reported_sloped_area,
            "tolerancePercent": settings["facetAreaTolerancePercent"],
            "unit": "sq ft",
            "reason": "The report does not provide both projected area and predominant pitch.",
        }
    )
    checks = [scale_check, aggregate_check, *edge_checks]
    checks.append(
        validate_geometry_counts(
            recovered_facets=len(polygons),
            expected_facets=int(roof_planes) if roof_planes is not None else None,
        )
    )
    warnings = [
        "Roof Manager reports 31 planes but supplies no per-plane pitches or downslope directions.",
        (
            "The 8:12 common-pitch and common-eave assumption is diagnostic only; "
            "the lower-eave section stays separate and no plane height is inferred."
        ),
        (
            "Supply pitch and downslope direction for every plane, review the outlines, "
            "and provide lower-eave constraints before approval."
        ),
    ]
    if len(polygons) != roof_planes:
        warnings.append(
            f"The selected vector line network polygonized into {len(polygons)} closed "
            f"regions, while the report states {roof_planes} planes."
        )
    if polygon_diagnostics:
        warnings.extend(
            str(item.get("message", "Unclosed vector outline detected."))
            for item in polygon_diagnostics
        )

    facets = [
        {
            "id": f"roof-manager-facet-{index:02d}",
            "name": f"Roof Manager region {index}",
            "roofType": "pitched",
            "roofTypeSource": "review placeholder; report does not tag each plane",
            "areaLabelSqFt": None,
            "polygonPagePoints": [[round(x, 3), round(y, 3)] for x, y in polygon],
            "pitch": None,
            "downslopeDirectionPage": None,
            "edgeTypes": _polygon_edge_types(polygon, diagram),
            "drawOrder": index,
            "source": "closed_region_from_legend_classified_vector_edges",
            "confidence": "low",
        }
        for index, polygon in enumerate(polygons, start=1)
    ]
    calibration = _calibration_record(
        selected_fit,
        chosen_hypothesis,
        settings,
    )
    if calibration["feetPerPoint"] is None:
        warnings.append(
            "No scale was stored because page-dimension labels did not produce a unique robust fit."
        )
    if scale_check["status"] != "pass":
        warnings.append(
            "Dimension-label calibration did not meet the configured residual "
            "and inlier thresholds."
        )
    failing_edge_checks = [check for check in edge_checks if check["status"] != "pass"]
    if failing_edge_checks:
        warnings.append(
            "One or more calibrated edge-class totals disagree with the report; "
            "review the numeric results before using scale."
        )
    page_dashes = [
        drawing
        for drawing in drawing_page.drawings
        if drawing.get("dashes") not in (None, "[] 0", "[]")
    ]
    return {
        "status": "needs_review",
        "confidence": "low",
        "coordinatePageNumber": drawing_page.page_number,
        "coordinatePageSize": {
            "widthPoints": drawing_page.width,
            "heightPoints": drawing_page.height,
        },
        "pageCalibrations": {
            **calibration,
            "method": calibration["method"],
            "feetPerPoint": calibration["feetPerPoint"],
            "hypothesisChosen": calibration["hypothesisChosen"],
            "inlierCount": calibration["inlierCount"],
            "residualsFeet": calibration["residualsFeet"],
            "edgesUsed": calibration["edgesUsed"],
            "tolerances": {
                "scalePercent": settings["scaleTolerancePercent"],
                "facetAreaPercent": settings["facetAreaTolerancePercent"],
                "classTotalPercent": settings["classTotalTolerancePercent"],
            },
        },
        "facets": facets,
        "facetCount": len(polygons),
        "expectedFacetCount": roof_planes,
        "candidatePlanPolygonsPagePoints": [
            {
                "id": f"roof-manager-region-{index + 1:02d}",
                "vertices": [[round(x, 3), round(y, 3)] for x, y in polygon],
                "areaSquarePoints": round(_polygon_area(polygon), 3),
                "coordinateUnits": "PDF points; not calibrated roof feet",
            }
            for index, polygon in enumerate(polygons)
        ],
        "reported": {
            "totalRoofAreaSqFt": total_area,
            "projectedAreaSqFt": reported_projection,
            "roofPlaneCount": roof_planes,
            "predominantPitch": (
                f"{predominant_pitch[0]}:{predominant_pitch[1]}" if predominant_pitch else None
            ),
            "edgeTotalsFt": report_edge_lengths,
        },
        "validation": {
            "checks": checks,
            "summary": summarize_checks(checks),
            "scaleHypotheses": {
                "plan": plan_fit.as_dict(),
                "sloped": sloped_fit.as_dict(),
                "chosen": chosen_hypothesis,
                "edgeTotalsPlanHypothesis": plan_edge_totals,
                "edgeTotalsSlopedHypothesis": sloped_edge_totals,
            },
            "tolerances": {
                "scalePercent": settings["scaleTolerancePercent"],
                "perFacetAreaPercent": settings["facetAreaTolerancePercent"],
                "edgeClassTotalPercent": settings["classTotalTolerancePercent"],
            },
        },
        "geometry": None,
        "warnings": list(dict.fromkeys(warnings)),
        "diagnostics": {
            "drawingPage": _page_diagnostic(drawing_page, diagram, len(polygons)),
            "areaPage": _page_diagnostic(area_page, [], 0),
            "polygonization": polygon_diagnostics,
            "dimensionLabels": {
                "candidateCount": len(dimension_labels),
                "associatedCount": sum(
                    match.segment_id is not None for match in selected_fit.matches
                )
                if selected_fit
                else 0,
                "inlierCount": selected_fit.inlier_count if selected_fit else 0,
                "outlierCount": len(dimension_labels) - selected_fit.inlier_count
                if selected_fit
                else len(dimension_labels),
                "unmatchedCount": len(dimension_labels)
                - sum(match.segment_id is not None for match in selected_fit.matches)
                if selected_fit
                else len(dimension_labels),
            },
            "dashedLineEvidence": {
                "legendClass": "lower_eave"
                if "lower_eave" in drawing_page.color_legend.values()
                else None,
                "meaning": (
                    "The page-local legend labels the light-blue swatch Lower Eave 1; "
                    "PyMuPDF exposed no separate dashed style for classified paths."
                ),
                "dashedDrawingCount": len(page_dashes),
            },
            "unmatchedLabels": ("This report format has no per-plane pitch or direction labels."),
        },
        "assumptions": [
            "A uniform 8:12 pitch is checked against aggregate area as a hypothesis only.",
            "Roof Manager page 3 has plan linework, but not enough constraints "
            "to solve plane heights.",
            "Absolute eave height and wall depth are not measured by this report.",
        ],
        "uniformPitchHypothesis": uniform_pitch,
        "manualInputRequired": [
            (
                f"Review {len(polygons)} closed polygon candidates and trace missing regions "
                f"to reach the reported "
                f"{int(roof_planes) if roof_planes is not None else 'unknown'} planes"
            ),
            "Pitch and downslope direction for each roof plane",
            "Confirm whether the 8:12 hypothesis applies to each plane; give the "
            "lower-eave section separate height constraints",
        ],
        "edgeClassTotals": {
            "drawnFeet": drawn_edge_totals,
            "reportedFeet": report_edge_lengths,
        },
        "dimensionCalibrationHypotheses": {
            "plan": plan_fit.as_dict(),
            "sloped": sloped_fit.as_dict(),
            "chosen": chosen_hypothesis,
        },
    }


def _dimension_observations(
    page: VectorPage, segments: Sequence[CalibrationSegment]
) -> list[DimensionObservation]:
    observations: list[DimensionObservation] = []
    for index, word in enumerate(
        numeric_words(
            page,
            min_y_fraction=0.08,
            max_y_fraction=0.68,
            min_x_fraction=0.05,
            max_x_fraction=0.95,
        )
    ):
        value = float(word.text)
        if value <= 0 or value > 99:
            continue
        nearest = min(
            (
                (segment_distance(word.center, segment.start, segment.end), segment)
                for segment in segments
            ),
            default=(float("inf"), None),
            key=lambda item: (item[0], item[1].id if item[1] else ""),
        )
        distance, segment = nearest
        if segment is None or distance > 22:
            continue
        observations.append(
            DimensionObservation(
                id=f"p{page.page_number}-label-{index:03d}",
                value_feet=value,
                location=word.center,
                page_number=page.page_number,
                edge_class=segment.edge_class,
                rounding_step_feet=1.0,
                rounded_up=False,
            )
        )
    return observations


def _drawn_class_totals(
    segments: Sequence[CalibrationSegment], scale: float | None, hypothesis: str
) -> dict[str, float]:
    if scale is None:
        return {}
    totals: dict[str, float] = {}
    seen: set[tuple[str, tuple[int, int], tuple[int, int]]] = set()
    for segment in segments:
        if not segment.edge_class:
            continue
        endpoints = sorted(
            (
                (round(segment.start[0] * 100), round(segment.start[1] * 100)),
                (round(segment.end[0] * 100), round(segment.end[1] * 100)),
            )
        )
        key = (segment.edge_class, endpoints[0], endpoints[1])
        if key in seen:
            continue
        seen.add(key)
        factor = segment.sloped_factor if hypothesis == "sloped" else 1.0
        totals[segment.edge_class] = (
            totals.get(segment.edge_class, 0.0) + segment.length_points * scale * factor
        )
    return {key: round(value, 3) for key, value in totals.items()}


def _combine_lower_eave(totals: dict[str, float]) -> dict[str, float]:
    combined = dict(totals)
    lower_eave = combined.pop("lower_eave", 0.0)
    combined["eave"] = combined.get("eave", 0.0) + lower_eave
    return {key: round(value, 3) for key, value in combined.items()}


def _polygon_edge_types(
    polygon: Sequence[tuple[float, float]], segments: Sequence[Any]
) -> list[str]:
    result: list[str] = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        candidates: list[tuple[float, str]] = []
        for segment in segments:
            if not segment.edge_class:
                continue
            other_angle = math.atan2(
                segment.end[1] - segment.start[1], segment.end[0] - segment.start[0]
            )
            angle_delta = abs((angle - other_angle + math.pi / 2) % math.pi - math.pi / 2)
            distance = segment_distance(midpoint, segment.start, segment.end)
            if distance <= 2.0 and angle_delta <= math.radians(8):
                candidates.append((distance + angle_delta * 10, segment.edge_class))
        edge_type = min(candidates, default=(float("inf"), "other"))[1]
        result.append("eave" if edge_type == "lower_eave" else edge_type)
    return result


def _scale_check(fit: Any, tolerance_percent: float) -> dict[str, Any]:
    if fit is None or fit.feet_per_point is None:
        return {
            "name": "dimension_scale",
            "status": "not_evaluable",
            "actual": None,
            "expected": None,
            "tolerancePercent": tolerance_percent,
            "unit": "ft/pt",
            "reason": fit.reason if fit else "Plan/sloped scale hypotheses did not select one fit.",
        }
    labels = [match.label_feet for match in fit.matches if match.segment_id]
    residual_percent = (
        fit.residual_rms_feet / (sum(labels) / len(labels)) * 100
        if fit.residual_rms_feet is not None and labels and sum(labels) > 0
        else None
    )
    inlier_percent = fit.inlier_count / fit.observation_count * 100 if fit.observation_count else 0
    passes = (
        fit.status == "calibrated"
        and inlier_percent >= 70
        and residual_percent is not None
        and residual_percent <= tolerance_percent
    )
    return {
        "name": "dimension_scale",
        "status": "pass" if passes else "warn",
        "actual": round(fit.feet_per_point, 8),
        "expected": None,
        "tolerancePercent": tolerance_percent,
        "unit": "ft/pt",
        "inlierCount": fit.inlier_count,
        "observationCount": fit.observation_count,
        "inlierPercent": round(inlier_percent, 3),
        "rSquared": fit.r_squared,
        "residualRmsFeet": fit.residual_rms_feet,
        "residualRmsPercent": round(residual_percent, 3) if residual_percent is not None else None,
        "reason": fit.reason if not passes else None,
    }


def _calibration_record(fit: Any, hypothesis: str, settings: dict[str, float]) -> dict[str, Any]:
    matches = [match for match in fit.matches if match.segment_id] if fit else []
    return {
        "method": (
            "Per-page legend class matching plus nearest numeric labels; robust plan/sloped fits"
        ),
        "feetPerPoint": fit.feet_per_point if fit else None,
        "hypothesisChosen": hypothesis if hypothesis in {"plan", "sloped"} else None,
        "inlierCount": fit.inlier_count if fit else 0,
        "observationCount": fit.observation_count if fit else 0,
        "rSquared": fit.r_squared if fit else None,
        "residualRmsFeet": fit.residual_rms_feet if fit else None,
        "edgesUsed": sorted({match.segment_id for match in matches if match.segment_id}),
        "residualsFeet": [
            {
                "labelId": match.observation_id,
                "edgeId": match.segment_id,
                "labelFeet": match.label_feet,
                "residualFeet": match.residual_feet,
                "inlier": match.inlier,
            }
            for match in matches
        ],
        "tolerances": {
            "scalePercent": settings["scaleTolerancePercent"],
            "edgeClassTotalPercent": settings["classTotalTolerancePercent"],
        },
    }


def _number_near(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None


def _pitch_near(text: str) -> tuple[int, int] | None:
    match = re.search(r"Predominant Pitch\s*(\d+)\s*:\s*(\d+)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"Predominant Pitch\s*(\d+)\s*/\s*(\d+)", text, flags=re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _edge_totals(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for label, edge_class in (
        ("EAVE", "eave"),
        ("RAKE", "rake"),
        ("RIDGE", "ridge"),
        ("HIP", "hip"),
        ("VALLEY", "valley"),
    ):
        match = re.search(rf"\b{label}\s+(\d+(?:\.\d+)?)\s*LF", text, flags=re.IGNORECASE)
        if match:
            result[edge_class] = float(match.group(1))
    return result


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    return (
        abs(
            sum(
                points[index][0] * points[(index + 1) % len(points)][1]
                - points[(index + 1) % len(points)][0] * points[index][1]
                for index in range(len(points))
            )
        )
        / 2
        if len(points) >= 3
        else 0.0
    )


def _page_diagnostic(
    page: VectorPage, segments: Sequence[Any], polygon_count: int
) -> dict[str, Any]:
    return {
        "pageNumber": page.page_number,
        "title": page.title,
        "drawingCount": len(page.drawings),
        "vectorSegmentCount": len(segments) or len(page.segments),
        "wordCount": len(page.words),
        "closedPolygonCount": polygon_count,
        "legend": page.color_legend,
        "coordinatesArePagePoints": True,
    }
