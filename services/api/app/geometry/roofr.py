"""Roofr's vector Diagram, Length, Area, and Pitch/Direction page adapter."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from app.geometry.calibrate import (
    CalibrationSegment,
    DimensionObservation,
    fit_both_scale_hypotheses,
    register_outlines,
)
from app.geometry.diagram_extract import (
    VectorPage,
    VectorSegment,
    VectorWord,
    diagram_segments,
    numeric_words,
    polygon_from_drawing,
)
from app.geometry.topology import (
    Point2,
    point_in_polygon,
    polygon_area,
    polygon_centroid,
    polygonize_segments,
    segment_distance,
    split_segments_at_intersections,
)
from app.geometry.validate import (
    summarize_checks,
    validate_class_totals,
    validate_facet_areas,
    validate_pitch_gradients,
)


def extract_roofr(pages: Sequence[VectorPage], *, settings: dict[str, float]) -> dict[str, Any]:
    diagram_page = _page(pages, "diagram")
    length_page = _page(pages, "length measurement report")
    area_page = _page(pages, "area measurement report")
    pitch_page = _page(pages, "pitch & direction measurement report")
    missing = [
        name
        for name, page in (
            ("Diagram", diagram_page),
            ("Length", length_page),
            ("Area", area_page),
            ("Pitch & direction", pitch_page),
        )
        if page is None
    ]
    if missing:
        return {
            "status": "unsupported",
            "reason": "Missing required Roofr report page(s): " + ", ".join(missing),
            "facets": [],
            "warnings": [],
            "diagnostics": {"missingPages": missing},
        }
    assert diagram_page and length_page and area_page and pitch_page
    report_text = "\n".join(page.text for page in pages)
    # Structure summaries appear before the report-wide summary in newer Roofr
    # reports. Use the last explicit total so a per-structure count is not
    # mistaken for the number of facets in the whole project.
    expected_facet_matches = list(
        re.finditer(r"Total roof facets\s+(\d+)\s+facets", report_text, flags=re.I)
    )
    if expected_facet_matches:
        expected_facet_count = int(expected_facet_matches[-1].group(1))
    else:
        expected_facet_match = re.search(r"\b(\d+)\s+facets\b", report_text, flags=re.I)
        expected_facet_count = int(expected_facet_match.group(1)) if expected_facet_match else None
    if not all(
        page.vector_supported for page in (diagram_page, length_page, area_page, pitch_page)
    ):
        unsupported = [
            page.page_number
            for page in (diagram_page, length_page, area_page, pitch_page)
            if not page.vector_supported
        ]
        return {
            "status": "unsupported",
            "reason": (
                f"Roofr diagram pages {unsupported} are raster-only; "
                "vector coordinates are unavailable."
            ),
            "facets": [],
            "warnings": [],
            "diagnostics": {"rasterOnlyPages": unsupported},
        }

    area_deleted_note = _deleted_facet_note(area_page)
    pitch_deleted_note = _deleted_facet_note(pitch_page)
    area_dashed_count = _dashed_drawing_count(area_page)
    pitch_dashed_count = _dashed_drawing_count(pitch_page)
    area_polygons = _extract_roofr_polygons(
        area_page,
        snap_tolerance=settings["snapTolerancePoints"],
        omit_deleted_dashed=bool(area_deleted_note),
        node_intersections=True,
        minimum_area=1.0,
    )
    pitch_polygons = _extract_roofr_polygons(
        pitch_page,
        snap_tolerance=settings["snapTolerancePoints"],
        omit_deleted_dashed=bool(pitch_deleted_note),
    )
    facets, area_warnings = _assign_area_labels(
        area_page, area_polygons, expected_facet_count=expected_facet_count
    )
    arrow_assignments, arrow_warnings = _assign_pitch_arrows(
        pitch_page, pitch_polygons, area_polygons, facets, settings["snapTolerancePoints"]
    )
    facets_by_id = {facet["id"]: facet for facet in facets}
    for facet_id, assignment in arrow_assignments.items():
        if facet_id in facets_by_id:
            facets_by_id[facet_id].update(assignment)
            pitch = assignment.get("pitch")
            if isinstance(pitch, dict) and facet_id in facets_by_id:
                rise = float(pitch.get("rise", 0))
                if rise == 0:
                    facets_by_id[facet_id]["roofType"] = "flat"
                    facets_by_id[facet_id]["roofTypeSource"] = "zero_pitch_label"
                elif facets_by_id[facet_id].get("areaLabelSqFt") is None:
                    facets_by_id[facet_id]["roofType"] = "pitched"
                    facets_by_id[facet_id]["roofTypeSource"] = "nonzero_pitch_label"

    page5_bounds = _polygon_bounds(pitch_polygons)
    page4_bounds = _polygon_bounds(area_polygons)
    pitch_to_area = (
        register_outlines(page5_bounds, page4_bounds) if page5_bounds and page4_bounds else None
    )
    length_segments = _dimension_segments(length_page)
    dimension_labels = _dimension_observations(length_page, length_segments)
    length_bounds = _vector_bounds(length_segments)
    area_outline_bounds = _polygon_bounds(area_polygons)
    length_to_area = (
        register_outlines(length_bounds, area_outline_bounds)
        if length_bounds and area_outline_bounds
        else None
    )

    sloped_factors_available = _attach_sloped_factors(
        length_segments,
        area_polygons,
        facets,
        length_to_area,
    )
    plan_fit, sloped_fit, selected_hypothesis = fit_both_scale_hypotheses(
        dimension_labels,
        length_segments,
        max_label_distance_points=26,
        minimum_inliers=3,
    )
    selected_fit = (
        plan_fit
        if selected_hypothesis == "plan"
        else sloped_fit
        if selected_hypothesis == "sloped"
        else None
    )
    page4_feet_per_point = (
        selected_fit.feet_per_point / length_to_area.scale
        if selected_fit and selected_fit.feet_per_point is not None and length_to_area
        else None
    )
    calibration_residuals = (
        [
            {
                "labelId": match.observation_id,
                "edgeId": match.segment_id,
                "labelFeet": match.label_feet,
                "predictedFeet": match.predicted_feet,
                "residualFeet": match.residual_feet,
                "inlier": match.inlier,
            }
            for match in selected_fit.matches
            if selected_fit and match.segment_id is not None
        ]
        if selected_fit
        else []
    )

    for facet in facets:
        facet["feetPerPoint"] = page4_feet_per_point
        facet["polygonFeet"] = (
            _page_polygon_to_plan(
                facet["polygonPagePoints"], page4_feet_per_point, area_outline_bounds
            )
            if page4_feet_per_point and area_outline_bounds
            else None
        )

    source_totals = _roofr_length_totals(length_page.text)
    drawn_totals = _drawn_class_totals(
        length_segments,
        selected_fit.feet_per_point if selected_fit else None,
        sloped_lengths=selected_hypothesis == "sloped",
    )
    class_checks = (
        validate_class_totals(
            drawn_totals,
            source_totals,
            tolerance_percent=settings["classTotalTolerancePercent"],
        )
        if drawn_totals
        else []
    )
    area_checks = validate_facet_areas(
        facets,
        feet_per_point=page4_feet_per_point,
        tolerance_percent=settings["facetAreaTolerancePercent"],
    )
    length_check = _scale_check(selected_fit, settings["scaleTolerancePercent"])
    registration_check = _registration_check(
        length_to_area,
        settings["scaleTolerancePercent"],
        name="page_registration_length_to_area",
    )
    pitch_registration_check = _registration_check(
        pitch_to_area,
        settings["scaleTolerancePercent"],
        name="page_registration_pitch_to_area",
    )
    checks = [
        length_check,
        registration_check,
        pitch_registration_check,
        *area_checks,
        *class_checks,
    ]
    validation: dict[str, Any] = {
        "checks": checks,
        "summary": summarize_checks(checks),
        "scaleHypotheses": {
            "plan": plan_fit.as_dict(),
            "sloped": sloped_fit.as_dict(),
            "chosen": selected_hypothesis,
            "slopedEdgeFactorsAvailable": sloped_factors_available,
        },
        "tolerances": {
            "scalePercent": settings["scaleTolerancePercent"],
            "perFacetAreaPercent": settings["facetAreaTolerancePercent"],
            "edgeClassTotalPercent": settings["classTotalTolerancePercent"],
        },
    }
    warnings = [*area_warnings, *arrow_warnings]
    if not selected_fit or not selected_fit.feet_per_point:
        warnings.append(
            "The PDF has no scale bar and dimension labels did not calibrate "
            "a stable feet-per-point scale."
        )
    if expected_facet_count is not None and len(facets) != expected_facet_count:
        warnings.append(
            f"The area drawing produced {len(facets)} facet polygons; "
            f"the report states {expected_facet_count}."
        )
    unresolved = [
        facet["id"]
        for facet in facets
        if not facet.get("pitch") or not facet.get("downslopeDirectionPage")
    ]
    if unresolved:
        warnings.append(
            "Pitch or arrow direction is unassigned for facets: " + ", ".join(unresolved) + "."
        )
    if selected_hypothesis == "ambiguous":
        warnings.append(
            "Plan-length and sloped-length hypotheses fit similarly; calibration needs review."
        )
    if selected_hypothesis == "none":
        warnings.append(
            "Neither plan-length nor sloped-length calibration met the configured inlier threshold."
        )
    if length_to_area and length_to_area.disagreement_percent > settings["scaleTolerancePercent"]:
        warnings.append(
            "Page registration width/height scales disagree by "
            f"{length_to_area.disagreement_percent:.2f}%, "
            f"above the {settings['scaleTolerancePercent']:.2f}% tolerance."
        )
    if not sloped_factors_available:
        warnings.append(
            "Hip/valley pitch factors could not be linked to all adjacent facets; "
            "the sloped-length hypothesis is incomplete."
        )

    canonical = _make_canonical_geometry(
        facets,
        area_outline_bounds,
        page4_feet_per_point,
        length_page,
        length_segments,
        length_to_area,
        snap_tolerance_points=settings["snapTolerancePoints"],
        height_tolerance_feet=settings["heightToleranceFeet"],
        wall_depth_feet=settings["wallDepthFeet"],
        area_tolerance_percent=settings["facetAreaTolerancePercent"],
    )
    if canonical:
        from app.geometry.heights import solve_plane_heights

        height_result = solve_plane_heights(
            {
                "vertices": canonical["vertices"],
                "planes": canonical["planes"],
                "edges": canonical["edges"],
            },
            eave_edge_ids={edge["id"] for edge in canonical["edges"] if edge["type"] == "eave"},
            overlap_order=canonical["overlapOrder"],
            tolerance_feet=settings["heightToleranceFeet"],
        )
        height_result["warnings"].append(
            "Absolute eave elevation is absent; the lowest detected eave is "
            "assigned the relative 0 ft datum."
        )
        if height_result["status"] == "solved":
            for vertex in canonical["vertices"]:
                if vertex["id"] in height_result["vertexHeights"]:
                    vertex["plan"][2] = height_result["vertexHeights"][vertex["id"]]
        canonical["heightSolve"] = height_result
        warnings.extend(height_result["warnings"])
        warnings.extend(height_result["failures"])
        checks.extend(
            validate_pitch_gradients(
                canonical,
                tolerance_degrees=settings["pitchToleranceDegrees"],
            )
        )
        canonical["status"] = "needs_review"
        canonical["confidence"] = "medium" if validation["summary"]["failureCount"] == 0 else "low"
        canonical["estimatedHeights"] = True
        canonical["validationWarnings"] = list(dict.fromkeys(warnings))
        validation["checks"] = checks
        validation["summary"] = summarize_checks(checks)
    return {
        "status": "needs_review",
        "confidence": "medium"
        if validation["summary"]["failureCount"] == 0 and not unresolved
        else "low",
        "coordinatePageNumber": area_page.page_number,
        "coordinatePageSize": {"widthPoints": area_page.width, "heightPoints": area_page.height},
        "pageCalibrations": {
            "lengthPageNumber": length_page.page_number,
            "areaPageNumber": area_page.page_number,
            "pitchPageNumber": pitch_page.page_number,
            "feetPerPoint": page4_feet_per_point,
            "lengthPageFeetPerPoint": selected_fit.feet_per_point if selected_fit else None,
            "hypothesisChosen": selected_hypothesis
            if selected_hypothesis in {"plan", "sloped"}
            else None,
            "inlierCount": selected_fit.inlier_count if selected_fit else 0,
            "observationCount": selected_fit.observation_count
            if selected_fit
            else len(dimension_labels),
            "rSquared": selected_fit.r_squared if selected_fit else None,
            "residualRmsFeet": selected_fit.residual_rms_feet if selected_fit else None,
            "edgesUsed": sorted({row["edgeId"] for row in calibration_residuals if row["edgeId"]}),
            "residualsFeet": calibration_residuals,
            "tolerances": {
                "scalePercent": settings["scaleTolerancePercent"],
                "facetAreaPercent": settings["facetAreaTolerancePercent"],
                "classTotalPercent": settings["classTotalTolerancePercent"],
            },
            "originPagePoint": [area_outline_bounds[0], area_outline_bounds[3]]
            if area_outline_bounds
            else None,
            "yAxis": "inverted_pdf_y",
            "lengthToArea": length_to_area.as_dict() if length_to_area else None,
            "pitchToArea": pitch_to_area.as_dict() if pitch_to_area else None,
            "method": (
                "Dimension-label robust fit; outline-bounds similarity registration; "
                "no page scale bar."
            ),
        },
        "facets": facets,
        "facetCount": len(facets),
        "expectedFacetCount": expected_facet_count,
        "edgeClassTotals": {
            "drawnFeet": drawn_totals,
            "reportedFeet": source_totals,
        },
        "reported": _roofr_reported_totals(area_page.text),
        "validation": validation,
        "geometry": canonical,
        "warnings": list(dict.fromkeys(warnings)),
        "diagnostics": {
            "diagramPage": _page_observation(diagram_page),
            "lengthPage": _page_observation(length_page),
            "areaPage": _page_observation(area_page),
            "pitchPage": _page_observation(pitch_page),
            "unmatchedAreaLabels": [
                warning
                for warning in area_warnings
                if "not matched" in warning or "ambiguous" in warning
            ],
            "unlabeledOutlineCount": sum(
                facet.get("areaLabelSqFt") is None for facet in facets
            ),
            "unmatchedPitchLabels": [
                warning
                for warning in arrow_warnings
                if "not matched" in warning
                or "ambiguous" in warning
                or "no uniquely matched direction arrow" in warning
            ],
            "dashedLineEvidence": {
                "areaPageFootnotes": area_deleted_note,
                "pitchPageFootnotes": pitch_deleted_note,
                "areaPageDashedDrawingCount": area_dashed_count,
                "pitchPageDashedDrawingCount": pitch_dashed_count,
                "dashedFacetPolicy": (
                    "Dashed paths are excluded only when page text identifies deleted facets; "
                    "no dimensions or pitches are inferred from them."
                ),
            },
        },
        "assumptions": [
            "Roofr area labels are rounded; comparison uses a one-sided 1 sq ft interval.",
            "A relative eave datum is used only when the vector legend identifies eaves.",
            "Absolute eave and wall heights are estimates and are not reported measurements.",
        ],
    }


def _page(pages: Sequence[VectorPage], title_substring: str) -> VectorPage | None:
    needle = title_substring.lower()
    return next((page for page in pages if needle in page.text.lower()), None)


def _extract_roofr_polygons(
    page: VectorPage,
    *,
    snap_tolerance: float,
    omit_deleted_dashed: bool = False,
    node_intersections: bool = False,
    minimum_area: float = 15.0,
) -> list[list[Point2]]:
    # Roofr often draws one outline as several independent vector paths. Build
    # faces from the page's line network so shared boundaries and small attached
    # facets close correctly. The report's deleted facets are dashed; exclude
    # those paths only when the page itself says dashed outlines are deleted.
    segments = diagram_segments(
        page,
        min_y_fraction=0.18,
        max_y_fraction=0.88,
        min_x_fraction=0.08,
        max_x_fraction=0.94,
        minimum_length=0.5,
    )
    if omit_deleted_dashed:
        segments = [segment for segment in segments if not _is_dashed_segment(segment)]

    # Diagram outlines use a consistent stroke color, while pitch labels and
    # arrows are separate colors. Polygonize each stroke-color network and pick
    # the one with the strongest closed-outline evidence. Filled glyphs are
    # intentionally excluded because they can make text into tiny faces.
    by_color: dict[tuple[int, int, int], list[VectorSegment]] = defaultdict(list)
    for segment in segments:
        if segment.color is None:
            continue
        color = tuple(round(channel * 1000) for channel in segment.color)
        by_color[color].append(segment)

    candidates: list[tuple[int, float, list[list[Point2]]]] = []
    for color_segments in by_color.values():
        linework = [segment.segment for segment in color_segments]
        if node_intersections:
            linework = split_segments_at_intersections(linework)
        polygons, _ = polygonize_segments(
            linework,
            snap_tolerance=snap_tolerance,
            minimum_area=minimum_area,
        )
        polygons = [polygon for polygon in polygons if polygon_area(polygon) >= minimum_area]
        if polygons:
            candidates.append(
                (len(polygons), sum(polygon_area(polygon) for polygon in polygons), polygons)
            )

    if not candidates:
        return []
    _, _, best_polygons = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return _deduplicate_polygons(best_polygons, snap_tolerance)


def _is_dashed_drawing(drawing: dict[str, Any]) -> bool:
    return drawing.get("dashes") not in (None, "[] 0", "[]")


def _is_dashed_segment(segment: VectorSegment) -> bool:
    return segment.dashes not in (None, "[] 0", "[]")


def _dashed_drawing_count(page: VectorPage) -> int:
    return sum(_is_dashed_drawing(drawing) for drawing in page.drawings)


def _deleted_facet_note(page: VectorPage) -> list[str]:
    return [
        line.strip()
        for line in page.text.splitlines()
        if "dash" in line.lower() and "delet" in line.lower()
    ]


def _deduplicate_polygons(polygons: Sequence[list[Point2]], tolerance: float) -> list[list[Point2]]:
    unique: dict[tuple[tuple[int, int], ...], list[Point2]] = {}
    for polygon in polygons:
        key = tuple(
            sorted((round(point[0] / tolerance), round(point[1] / tolerance)) for point in polygon)
        )
        unique[key] = polygon
    return sorted(
        unique.values(),
        key=lambda polygon: (
            min(point[1] for point in polygon),
            min(point[0] for point in polygon),
            polygon_area(polygon),
        ),
    )


def _assign_area_labels(
    page: VectorPage,
    polygons: Sequence[list[Point2]],
    *,
    expected_facet_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    numbers = _area_label_words(page, polygons)
    flat_labels = [
        word
        for word in page.words
        if word.text.lower() == "flat" and word.center[1] > page.height * 0.22
    ]
    records: list[dict[str, Any]] = []
    used_polygons: set[int] = set()
    for word in numbers:
        candidates = [
            index
            for index, polygon in enumerate(polygons)
            if _label_touches_polygon(word.center, polygon, tolerance=6.0)
        ]
        candidates.sort(key=lambda index: polygon_area(polygons[index]))
        available = [index for index in candidates if index not in used_polygons]
        if not available:
            message = "ambiguous" if candidates else "not matched"
            warnings.append(
                f"Area label {word.text} at {word.center} was {message} "
                "to one unused facet polygon."
            )
            continue
        # The report intentionally overlays small facets (notably the 12 and
        # 18 sq ft strips) on larger outlines. The smallest containing closed
        # outline is the directly supported label polygon; retain the overlap
        # diagnostic instead of assigning the label to the enclosing outline.
        polygon_index = available[0]
        used_polygons.add(polygon_index)
        flat_word = min(
            flat_labels,
            key=lambda candidate: math.dist(candidate.center, word.center),
            default=None,
        )
        roof_type = (
            "flat" if flat_word and math.dist(flat_word.center, word.center) <= 26 else "pitched"
        )
        records.append(
            {
                "id": f"roofr-facet-{len(records) + 1:02d}",
                "name": f"Roofr facet {len(records) + 1}",
                "sourceFormat": "roofr",
                "pageNumber": page.page_number,
                "areaLabelSqFt": float(word.text),
                "areaRoundedUp": True,
                "roofType": roof_type,
                "roofTypeSource": (
                    "nearby_flat_label" if roof_type == "flat" else "pitched_placeholder"
                ),
                "polygonPagePoints": [
                    [round(x, 5), round(y, 5)] for x, y in polygons[polygon_index]
                ],
                "labelPagePoint": [round(word.center[0], 5), round(word.center[1], 5)],
                "pitch": None,
                "downslopeDirectionPage": None,
                "drawOrder": _polygon_source_draw_order(page, polygons[polygon_index]),
                "drawOrderSource": "pdf_content_stream"
                if _polygon_source_draw_order(page, polygons[polygon_index]) is not None
                else None,
                "source": "vector_outline_and_selectable_area_label",
                "confidence": "medium",
                "overlapCandidates": len(candidates),
            }
        )
    unlabeled_polygons = [index for index in range(len(polygons)) if index not in used_polygons]
    keep_unlabeled = expected_facet_count is not None and len(polygons) == expected_facet_count
    retained_unlabeled = unlabeled_polygons if keep_unlabeled else []
    for polygon_index in retained_unlabeled:
        facet_number = len(records) + 1
        records.append(
            {
                "id": f"roofr-facet-{facet_number:02d}",
                "name": f"Roofr outline candidate {facet_number}",
                "sourceFormat": "roofr",
                "pageNumber": page.page_number,
                "areaLabelSqFt": None,
                "areaRoundedUp": None,
                "roofType": "pitched",
                "roofTypeSource": "unresolved_outline_placeholder",
                "polygonPagePoints": [
                    [round(x, 5), round(y, 5)] for x, y in polygons[polygon_index]
                ],
                "labelPagePoint": None,
                "pitch": None,
                "downslopeDirectionPage": None,
                "drawOrder": _polygon_source_draw_order(page, polygons[polygon_index]),
                "drawOrderSource": "pdf_content_stream"
                if _polygon_source_draw_order(page, polygons[polygon_index]) is not None
                else None,
                "source": "closed_vector_outline_without_matched_area_label",
                "confidence": "low",
                "overlapCandidates": 0,
            }
        )
    if retained_unlabeled:
        warnings.append(
            f"{len(retained_unlabeled)} closed vector outline(s) have no reliable area label; "
            "they remain available for Geometry review."
        )
    elif unlabeled_polygons and (
        expected_facet_count is None or len(records) != expected_facet_count
    ):
        warnings.append(
            f"{len(unlabeled_polygons)} extra closed vector region(s) did not match the "
            "report's facet count and were not promoted to roof facets."
        )
    return records, warnings


def _area_label_words(
    page: VectorPage, polygons: Sequence[list[Point2]]
) -> list[VectorWord]:
    """Read overlapping Roofr labels as separate area observations when geometry supports it."""
    words_by_bbox: dict[tuple[str, int, int, int, int], VectorWord] = {}
    for word in page.text_spans or page.words:
        value_text = word.text.replace(",", "")
        if not re.fullmatch(r"\d{1,6}", value_text):
            continue
        if not (
            page.width * 0.08 <= word.center[0] <= page.width * 0.94
            and page.height * 0.25 <= word.center[1] <= page.height * 0.88
        ):
            continue
        key = (
            value_text,
            round(word.bbox[0] * 100),
            round(word.bbox[1] * 100),
            round(word.bbox[2] * 100),
            round(word.bbox[3] * 100),
        )
        words_by_bbox[key] = VectorWord(value_text, word.bbox)

    expanded: list[VectorWord] = []
    for word in words_by_bbox.values():
        text = word.text
        split_repeated_pair = len(text) == 2 and text[0] == text[1]
        if len(text) < 4 and not split_repeated_pair:
            expanded.append(word)
            continue
        half = len(text) // 2
        repeated_halves = text[:half] == text[half:]
        stacked_text = word.bbox[3] - word.bbox[1] > 11.5
        if not (split_repeated_pair or len(text) > 4 or repeated_halves or stacked_text):
            expanded.append(word)
            continue

        midpoint_x = (word.bbox[0] + word.bbox[2]) / 2
        first = VectorWord(
            text[:half], (word.bbox[0], word.bbox[1], midpoint_x, word.bbox[3])
        )
        second = VectorWord(
            text[half:], (midpoint_x, word.bbox[1], word.bbox[2], word.bbox[3])
        )
        if repeated_halves or split_repeated_pair:
            split_pair = _similar_area_label_polygon_pair(
                first.center, second.center, polygons, tolerance=6.0
            )
        else:
            first_polygon = _nearest_label_polygon(first.center, polygons, tolerance=6.0)
            second_polygon = _nearest_label_polygon(second.center, polygons, tolerance=6.0)
            split_pair = (
                (first_polygon, second_polygon)
                if first_polygon is not None
                and second_polygon is not None
                and first_polygon != second_polygon
                else None
            )
        if split_pair is not None:
            expanded.extend((first, second))
        else:
            expanded.append(word)

    return sorted(expanded, key=lambda word: (word.center[1], word.center[0], word.text))


def _nearest_label_polygon(
    point: Point2, polygons: Sequence[Sequence[Point2]], *, tolerance: float
) -> int | None:
    candidates = [
        index
        for index, polygon in enumerate(polygons)
        if _label_touches_polygon(point, polygon, tolerance=tolerance)
    ]
    return min(
        candidates,
        key=lambda index: (
            0
            if point_in_polygon(point, polygons[index])
            else min(
                segment_distance(
                    point,
                    polygons[index][edge_index],
                    polygons[index][(edge_index + 1) % len(polygons[index])],
                )
                for edge_index in range(len(polygons[index]))
            ),
            polygon_area(polygons[index]),
            math.dist(point, polygon_centroid(polygons[index])),
        ),
        default=None,
    )


def _similar_area_label_polygon_pair(
    first_point: Point2,
    second_point: Point2,
    polygons: Sequence[Sequence[Point2]],
    *,
    tolerance: float,
) -> tuple[int, int] | None:
    first_candidates = [
        index
        for index, polygon in enumerate(polygons)
        if _label_touches_polygon(first_point, polygon, tolerance=tolerance)
    ]
    second_candidates = [
        index
        for index, polygon in enumerate(polygons)
        if _label_touches_polygon(second_point, polygon, tolerance=tolerance)
    ]
    pairs = [
        (first, second)
        for first in first_candidates
        for second in second_candidates
        if first != second
        and max(polygon_area(polygons[first]), polygon_area(polygons[second]))
        <= 1.5 * min(polygon_area(polygons[first]), polygon_area(polygons[second]))
    ]
    return min(
        pairs,
        key=lambda pair: (
            polygon_area(polygons[pair[0]]) + polygon_area(polygons[pair[1]]),
            math.dist(first_point, polygon_centroid(polygons[pair[0]]))
            + math.dist(second_point, polygon_centroid(polygons[pair[1]])),
        ),
        default=None,
    )


def _assign_pitch_arrows(
    pitch_page: VectorPage,
    pitch_polygons: Sequence[list[Point2]],
    area_polygons: Sequence[list[Point2]],
    facets: Sequence[dict[str, Any]],
    snap_tolerance: float,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not pitch_polygons or not area_polygons or not facets:
        return {}, ["Pitch page could not be registered against the area facets."]
    transform = register_outlines(_bounds(pitch_polygons), _bounds(area_polygons))
    arrows = _arrow_shapes(pitch_page)
    labels = numeric_words(
        pitch_page,
        min_y_fraction=0.18,
        max_y_fraction=0.84,
        min_x_fraction=0.15,
        max_x_fraction=0.85,
    )
    assignments: dict[str, dict[str, Any]] = {}
    label_targets: dict[int, str] = {}
    ambiguous_targets: set[int] = set()
    for label_index, label in enumerate(labels):
        pitch = int(label.text)
        if pitch not in {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12}:
            warnings.append(f"Unrecognized pitch label {label.text} remains unassigned.")
            continue
        area_point = transform.apply(label.center)
        containing = [
            facet
            for facet in facets
            if _label_touches_polygon(
                area_point,
                [tuple(point) for point in facet["polygonPagePoints"]],
                tolerance=2.5,
            )
        ]
        if not containing:
            warnings.append(
                f"Pitch label {label.text} at {label.center} is not inside an area facet."
            )
            continue
        containing.sort(
            key=lambda facet: polygon_area([tuple(point) for point in facet["polygonPagePoints"]])
        )
        target = containing[0]
        if target["id"] in assignments:
            ambiguous_targets.add(label_index)
            warnings.append(
                f"Multiple pitch labels map to {target['id']}; the source pitch remains ambiguous."
            )
            assignments.pop(target["id"], None)
            continue
        label_targets[label_index] = str(target["id"])
        assignments[str(target["id"])] = {
            "pitch": {
                "notation": f"{pitch}:12",
                "rise": float(pitch),
                "run": 12.0,
                "degrees": math.degrees(math.atan2(pitch, 12)),
            },
            "downslopeDirectionPage": None,
            "pitchLabelPagePoint": [round(label.center[0], 5), round(label.center[1], 5)],
            "pitchSource": "selectable_pitch_label",
        }

    # Clustered facets can place an arrow closer to the neighboring label; pair all
    # arrows and labels together so one local nearest match does not steal the next.
    max_arrow_label_distance = 36.0
    unmatched_cost = 30.0
    distances = [
        [math.dist(arrow["center"], label.center) for label in labels]
        for arrow in arrows
    ]
    assignment_costs = [
        [
            distance if distance <= max_arrow_label_distance else 1_000_000.0
            for distance in row
        ]
        + [unmatched_cost] * len(arrows)
        for row in distances
    ]
    matched_labels = _minimum_cost_assignment(assignment_costs)
    used_labels: set[int] = set()
    for arrow_index, arrow in enumerate(arrows):
        label_index = matched_labels[arrow_index] if matched_labels else -1
        if (
            label_index < 0
            or label_index >= len(labels)
            or distances[arrow_index][label_index] > max_arrow_label_distance
        ):
            warnings.append(
                f"Direction arrow at page point {arrow['center']} has no nearby pitch label."
            )
            continue
        label = labels[label_index]
        label_distance = distances[arrow_index][label_index]
        used_labels.add(label_index)
        facet_id = label_targets.get(label_index)
        if facet_id is None and label_index not in ambiguous_targets:
            area_point = transform.apply(arrow["center"])
            containing = [
                facet
                for facet in facets
                if _label_touches_polygon(
                    area_point,
                    [tuple(point) for point in facet["polygonPagePoints"]],
                    tolerance=2.5,
                )
            ]
            if containing:
                containing.sort(
                    key=lambda facet: polygon_area(
                        [tuple(point) for point in facet["polygonPagePoints"]]
                    )
                )
                facet_id = str(containing[0]["id"])
        if facet_id is None:
            message = "not matched" if label_index not in ambiguous_targets else "ambiguous"
            warnings.append(
                f"Pitch label {label.text} / arrow at {arrow['center']} was {message} "
                "to one area facet."
            )
            continue
        assignment = assignments.get(facet_id)
        if not assignment:
            warnings.append(f"Pitch arrow did not resolve the pitch label for {facet_id}.")
            continue
        if assignment.get("downslopeDirectionPage") is not None:
            warnings.append(
                f"More than one pitch arrow maps to {facet_id}; direction remains ambiguous."
            )
            assignment["downslopeDirectionPage"] = None
            continue
        assignment.update(
            {
                "downslopeDirectionPage": [
                    round(arrow["direction"][0], 6),
                    round(arrow["direction"][1], 6),
                ],
                "arrowPagePoint": [round(arrow["center"][0], 5), round(arrow["center"][1], 5)],
                "arrowLabelDistancePoints": round(label_distance, 3),
                "pitchSource": "selectable_pitch_label_and_vector_direction_arrow",
            }
        )
    for label_index, label in enumerate(labels):
        if label_index not in used_labels:
            warnings.append(
                f"Pitch label {label.text} at {label.center} has no uniquely "
                "matched direction arrow."
            )
    return assignments, warnings


def _minimum_cost_assignment(costs: Sequence[Sequence[float]]) -> list[int]:
    """Return the minimum-cost column for each row of a rectangular matrix."""
    row_count = len(costs)
    if row_count == 0:
        return []
    column_count = len(costs[0])
    if column_count < row_count or any(len(row) != column_count for row in costs):
        raise ValueError(
            "Assignment cost matrix must be rectangular with at least as many columns as rows."
        )

    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)
    for row_index in range(1, row_count + 1):
        matched_row[0] = row_index
        column = 0
        best_slack = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column] = True
            current_row = matched_row[column]
            delta = math.inf
            next_column = 0
            for candidate in range(1, column_count + 1):
                if used[candidate]:
                    continue
                reduced = (
                    costs[current_row - 1][candidate - 1]
                    - row_potential[current_row]
                    - column_potential[candidate]
                )
                if reduced < best_slack[candidate]:
                    best_slack[candidate] = reduced
                    previous_column[candidate] = column
                if best_slack[candidate] < delta:
                    delta = best_slack[candidate]
                    next_column = candidate
            for candidate in range(column_count + 1):
                if used[candidate]:
                    row_potential[matched_row[candidate]] += delta
                    column_potential[candidate] -= delta
                else:
                    best_slack[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            prior = previous_column[column]
            matched_row[column] = matched_row[prior]
            column = prior
            if column == 0:
                break

    result = [-1] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column] != 0:
            result[matched_row[column] - 1] = column - 1
    return result


def _arrow_shapes(page: VectorPage) -> list[dict[str, Any]]:
    arrows: list[dict[str, Any]] = []
    for drawing in page.drawings:
        rect = drawing.get("rect")
        if rect is None or len(drawing.get("items", [])) < 3:
            continue
        x0, y0, x1, y1 = (float(value) for value in rect)
        width, height = x1 - x0, y1 - y0
        if not (1.5 <= max(width, height) <= 12 and 1.5 <= min(width, height) <= 5):
            continue
        if (drawing.get("fill") or (0, 0, 0))[0] < 0.05:
            continue
        segments = [segment.segment for segment in _drawing_segments(page, drawing)]
        nodes: dict[tuple[int, int], tuple[Point2, int]] = {}
        unique_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for start, end in segments:
            first, second = _key_point(start), _key_point(end)
            if first == second:
                continue
            edge = (first, second) if first < second else (second, first)
            if edge in unique_edges:
                continue
            unique_edges.add(edge)
            nodes[first] = (start, nodes.get(first, (start, 0))[1] + 1)
            nodes[second] = (end, nodes.get(second, (end, 0))[1] + 1)
        tips = [point for point, degree in nodes.values() if degree >= 3]
        if not tips:
            continue
        tip = tips[0]
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        dx, dy = tip[0] - center[0], tip[1] - center[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        direction = (dx / length, dy / length)
        arrows.append(
            {"center": center, "tip": tip, "direction": direction, "bbox": [x0, y0, x1, y1]}
        )
    return sorted(arrows, key=lambda arrow: (arrow["center"][1], arrow["center"][0]))


def _drawing_segments(page: VectorPage, drawing: dict[str, Any]) -> list[VectorSegment]:
    from app.geometry.diagram_extract import drawing_segments

    return drawing_segments(drawing, page_number=page.page_number)


def _key_point(point: Point2) -> tuple[int, int]:
    return round(point[0] * 1000), round(point[1] * 1000)


def _polygon_signature_distance(first: Sequence[Point2], second: Sequence[Point2]) -> float:
    c1, c2 = polygon_centroid(first), polygon_centroid(second)
    area1, area2 = polygon_area(first), polygon_area(second)
    return math.dist(c1, c2) + abs(math.sqrt(area1) - math.sqrt(area2))


def _label_touches_polygon(point: Point2, polygon: Sequence[Point2], *, tolerance: float) -> bool:
    if point_in_polygon(point, polygon):
        return True
    return (
        min(
            segment_distance(point, polygon[index], polygon[(index + 1) % len(polygon)])
            for index in range(len(polygon))
        )
        <= tolerance
    )


def _dimension_observations(
    page: VectorPage, segments: Sequence[CalibrationSegment]
) -> list[DimensionObservation]:
    result: list[DimensionObservation] = []
    for index, word in enumerate(
        numeric_words(
            page,
            min_y_fraction=0.23,
            max_y_fraction=0.88,
            min_x_fraction=0.22,
            max_x_fraction=0.82,
        )
    ):
        if float(word.text) <= 0:
            continue
        nearest = min(
            (
                (segment_distance(word.center, segment.start, segment.end), segment)
                for segment in segments
                if segment.edge_class
            ),
            default=(float("inf"), None),
            key=lambda match: (match[0], match[1].id if match[1] else ""),
        )
        segment = nearest[1]
        result.append(
            DimensionObservation(
                id=f"p{page.page_number}-label-{index:02d}",
                value_feet=float(word.text),
                location=word.center,
                page_number=page.page_number,
                edge_class=segment.edge_class if segment and nearest[0] <= 26 else None,
                rounding_step_feet=1.0,
                rounded_up=True,
            )
        )
    return result


def _dimension_segments(page: VectorPage) -> list[CalibrationSegment]:
    segments = diagram_segments(
        page,
        min_y_fraction=0.22,
        max_y_fraction=0.88,
        min_x_fraction=0.2,
        max_x_fraction=0.82,
        require_legend_class=True,
        minimum_length=4,
    )
    return [
        CalibrationSegment(
            id=f"p{page.page_number}-d{segment.drawing_index}-i{segment.item_index}-{index}",
            start=segment.start,
            end=segment.end,
            page_number=segment.page_number,
            edge_class=segment.edge_class,
        )
        for index, segment in enumerate(segments)
    ]


def _attach_sloped_factors(
    segments: list[CalibrationSegment],
    polygons: Sequence[list[Point2]],
    facets: Sequence[dict[str, Any]],
    page_transform: Any,
) -> bool:
    if not page_transform:
        return False
    pitches_by_polygon: list[tuple[list[Point2], float]] = []
    for facet in facets:
        pitch = facet.get("pitch")
        if not isinstance(pitch, dict):
            continue
        polygon = [tuple(point) for point in facet["polygonPagePoints"]]
        pitches_by_polygon.append(
            (polygon, math.sqrt(1 + (float(pitch["rise"]) / float(pitch["run"])) ** 2))
        )
    found = 0
    for index, segment in enumerate(segments):
        if segment.edge_class not in {"rake", "hip", "valley"}:
            continue
        mapped = (page_transform.apply(segment.start), page_transform.apply(segment.end))
        factors: list[float] = []
        for polygon, factor in pitches_by_polygon:
            polygon_edges = [
                (polygon[i], polygon[(i + 1) % len(polygon)]) for i in range(len(polygon))
            ]
            if any(
                max(segment_distance(point, edge[0], edge[1]) for point in mapped) <= 4.0
                for edge in polygon_edges
            ):
                factors.append(factor)
        if factors:
            segments[index] = CalibrationSegment(
                segment.id,
                segment.start,
                segment.end,
                segment.page_number,
                segment.edge_class,
                sum(factors) / len(factors),
            )
            found += 1
    return found > 0


def _polygon_bounds(
    polygons: Sequence[Sequence[Point2]],
) -> tuple[float, float, float, float] | None:
    points = [point for polygon in polygons for point in polygon]
    return _bounds_points(points) if points else None


def _bounds(polygons: Sequence[Sequence[Point2]]) -> tuple[float, float, float, float]:
    points = [point for polygon in polygons for point in polygon]
    if not points:
        raise ValueError("No polygon points are available for registration")
    return _bounds_points(points)


def _bounds_points(points: Sequence[Point2]) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _vector_bounds(
    segments: Sequence[CalibrationSegment],
) -> tuple[float, float, float, float] | None:
    points = [point for segment in segments for point in (segment.start, segment.end)]
    return _bounds_points(points) if points else None


def _page_polygon_to_plan(
    polygon: Sequence[Sequence[float]],
    feet_per_point: float,
    bounds: tuple[float, float, float, float],
) -> list[list[float]]:
    return [
        [
            round((float(x) - bounds[0]) * feet_per_point, 6),
            round((bounds[3] - float(y)) * feet_per_point, 6),
        ]
        for x, y in polygon
    ]


def _scale_check(fit: Any, tolerance_percent: float) -> dict[str, Any]:
    if fit is None or fit.feet_per_point is None:
        return {
            "name": "dimension_scale",
            "status": "not_evaluable",
            "actual": None,
            "expected": None,
            "tolerancePercent": tolerance_percent,
            "unit": "ft/pt",
            "reason": fit.reason if fit else "No selected scale hypothesis.",
        }
    status = "pass" if fit.status == "calibrated" and fit.inlier_count >= 3 else "warn"
    return {
        "name": "dimension_scale",
        "status": status,
        "actual": round(fit.feet_per_point, 8),
        "expected": None,
        "tolerancePercent": tolerance_percent,
        "unit": "ft/pt",
        "inlierCount": fit.inlier_count,
        "observationCount": fit.observation_count,
        "rSquared": fit.r_squared,
        "residualRmsFeet": fit.residual_rms_feet,
        "reason": fit.reason,
    }


def _registration_check(
    transform: Any, tolerance_percent: float, *, name: str = "page_registration"
) -> dict[str, Any]:
    if transform is None:
        return {
            "name": name,
            "status": "not_evaluable",
            "actual": None,
            "expected": 0,
            "tolerancePercent": tolerance_percent,
            "unit": "%",
        }
    return {
        "name": name,
        "status": "pass" if transform.disagreement_percent <= tolerance_percent else "warn",
        "actual": round(transform.disagreement_percent, 4),
        "expected": 0,
        "tolerancePercent": tolerance_percent,
        "unit": "%",
        "widthScale": transform.width_ratio,
        "heightScale": transform.height_ratio,
    }


def _roofr_length_totals(text: str) -> dict[str, float]:
    labels = {
        "eaves": "eave",
        "valleys": "valley",
        "hips": "hip",
        "ridges": "ridge",
        "rakes": "rake",
        "wall flashing": "wall_flashing",
        "step flashing": "step_flashing",
        "curb flashing": "curb_flashing",
        "transitions": "transition",
        "unspecified": "unspecified",
    }
    result: dict[str, float] = {}
    for label, edge_class in labels.items():
        match = re.search(
            rf"{re.escape(label)}:\s*(\d+)\s*ft\s*(\d+)\s*in", text, flags=re.IGNORECASE
        )
        if match:
            result[edge_class] = int(match.group(1)) + int(match.group(2)) / 12
    return result


def _roofr_reported_totals(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for source_label, result_name in (
        ("total roof area", "totalRoofAreaSqFt"),
        ("pitched roof area", "pitchedRoofAreaSqFt"),
        ("flat roof area", "flatRoofAreaSqFt"),
    ):
        match = re.search(
            rf"{re.escape(source_label)}:\s*([\d,]+)\s*sqft\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            result[result_name] = float(match.group(1).replace(",", ""))
    return result


def _drawn_class_totals(
    segments: Sequence[CalibrationSegment],
    scale: float | None,
    *,
    sloped_lengths: bool = False,
) -> dict[str, float]:
    if scale is None:
        return {}
    totals: dict[str, float] = {}
    seen: set[tuple[str, tuple[int, int], tuple[int, int]]] = set()
    for segment in segments:
        if not segment.edge_class:
            continue
        first = (round(segment.start[0] * 100), round(segment.start[1] * 100))
        second = (round(segment.end[0] * 100), round(segment.end[1] * 100))
        endpoints = sorted((first, second))
        key = (segment.edge_class, endpoints[0], endpoints[1])
        if key in seen:
            continue
        seen.add(key)
        factor = segment.sloped_factor if sloped_lengths else 1.0
        totals[segment.edge_class] = (
            totals.get(segment.edge_class, 0.0) + segment.length_points * scale * factor
        )
    return {key: round(value, 3) for key, value in totals.items()}


def _make_canonical_geometry(
    facets: Sequence[dict[str, Any]],
    bounds: tuple[float, float, float, float] | None,
    feet_per_point: float | None,
    length_page: VectorPage,
    length_segments: Sequence[CalibrationSegment],
    length_to_area: Any,
    *,
    snap_tolerance_points: float,
    height_tolerance_feet: float,
    wall_depth_feet: float,
    area_tolerance_percent: float,
) -> dict[str, Any] | None:
    if not bounds or not feet_per_point:
        return None
    usable = [
        facet
        for facet in facets
        if isinstance(facet.get("polygonPagePoints"), Sequence)
        and len(facet["polygonPagePoints"]) >= 3
    ]
    if not usable or not any(facet.get("pitch") for facet in usable):
        return None

    resolved, inference_warnings = _resolve_facet_geometry(
        usable, snap_tolerance_points=snap_tolerance_points
    )

    plan_polygons: list[dict[str, Any]] = []
    for facet in usable:
        geometry_attributes = resolved[str(facet["id"])]
        plan_points = _page_polygon_to_plan(facet["polygonPagePoints"], feet_per_point, bounds)
        direction_page = geometry_attributes["directionPage"]
        direction_plan = (float(direction_page[0]), -float(direction_page[1]))
        length = math.hypot(*direction_plan)
        direction_plan = (direction_plan[0] / length, direction_plan[1] / length)
        pitch = geometry_attributes["pitch"]
        plan_polygons.append(
            {
                "id": facet["id"],
                "name": facet["name"],
                "roofType": "flat" if float(pitch["rise"]) == 0 else "pitched",
                "vertices": plan_points,
                "reportedAreaSqFt": facet["areaLabelSqFt"],
                "pitch": pitch,
                "downslopePlanUnitVector": direction_plan,
                "drawOrder": 0,
                "sourceDrawOrder": facet.get("drawOrder"),
                "provenance": {
                    "outline": "reported",
                    "pitch": geometry_attributes["pitchProvenance"],
                    "downslopeDirection": geometry_attributes["directionProvenance"],
                    "area": "reported" if facet.get("areaLabelSqFt") is not None else "inferred",
                },
                "confidence": 0.4
                if "inferred" in {
                    geometry_attributes["pitchProvenance"],
                    geometry_attributes["directionProvenance"],
                }
                else 0.72,
            }
        )
    if not plan_polygons:
        return None

    from app.geometry.topology import build_topology

    area_to_length = None
    # All coordinates in this adapter use the page-4 coordinate frame. Segment class
    # mapping is performed against the raw source page when drawing edge records.
    del area_to_length

    def classify_edge(start: Point2, end: Point2) -> str:
        if length_to_area is None:
            return "other"
        area_start = (bounds[0] + start[0] / feet_per_point, bounds[3] - start[1] / feet_per_point)
        area_end = (bounds[0] + end[0] / feet_per_point, bounds[3] - end[1] / feet_per_point)
        length_start = (
            (area_start[0] - length_to_area.translate_x) / length_to_area.scale,
            (area_start[1] - length_to_area.translate_y) / length_to_area.scale,
        )
        length_end = (
            (area_end[0] - length_to_area.translate_x) / length_to_area.scale,
            (area_end[1] - length_to_area.translate_y) / length_to_area.scale,
        )
        midpoint = ((length_start[0] + length_end[0]) / 2, (length_start[1] + length_end[1]) / 2)
        candidates: list[tuple[float, str]] = []
        edge_length = math.dist(length_start, length_end)
        if edge_length <= 1e-8:
            return "other"
        edge_angle = math.atan2(length_end[1] - length_start[1], length_end[0] - length_start[0])
        for segment in length_segments:
            if not segment.edge_class:
                continue
            segment_mid = (
                (segment.start[0] + segment.end[0]) / 2,
                (segment.start[1] + segment.end[1]) / 2,
            )
            distance = math.dist(midpoint, segment_mid)
            segment_angle = math.atan2(
                segment.end[1] - segment.start[1], segment.end[0] - segment.start[0]
            )
            angle_delta = abs((edge_angle - segment_angle + math.pi / 2) % math.pi - math.pi / 2)
            if distance <= 5.0 and angle_delta <= math.radians(8):
                candidates.append((distance + angle_delta * 10, segment.edge_class))
        return min(candidates, default=(float("inf"), "other"))[1]

    topology = build_topology(
        plan_polygons,
        snap_tolerance=max(feet_per_point * snap_tolerance_points, 0.005),
        edge_classifier=classify_edge,
    )
    overlap_order = _overlap_order(plan_polygons)
    return {
        "schemaVersion": "1.1",
        "geometryId": "roofr-extracted-candidate-v1",
        "status": "needs_review",
        "source": "extracted",
        "confidence": "low",
        "estimatedHeights": True,
        "units": "feet",
        "coordinateSystem": {
            "planAxes": "u is right and v is up on Roofr Area page 4",
            "heightAxis": "relative to the lowest detected eave; absolute elevation unavailable",
            "threeJsMapping": "X=u, Y=h, Z=-v",
            "orientation": "Preserves the report drawing orientation; no north-up rotation",
        },
        "compass": {
            "north": "-u",
            "east": "+v",
            "south": "+u",
            "west": "-v",
            "source": "Roofr compass printed on Diagram page 2",
        },
        "sourceReference": {
            "viewer": "",
            "report": "Roofr measurement PDF supplied with this repository",
            "method": (
                "PyMuPDF page vectors, dimension-label calibration, area-label matching, "
            "pitch-arrow matching, and review-only estimates for unresolved facets"
            ),
        },
        "heightModel": {
            "formula": "h = plane offset - (rise/run) * dot(plan, downslope direction)",
            "manualHeightOverrides": [],
            "flatDrainPitch": {"rise": 1, "run": 12, "direction": "Per Roofr vector arrow"},
            "wallGroundPlaneHeightFt": -wall_depth_feet,
            "wallGroundPlaneIsEstimated": True,
        },
        "vertices": topology["vertices"],
        "planes": topology["planes"],
        "edges": topology["edges"],
        "wallLoops": [],
        "overlapOrder": overlap_order,
        "omittedDiagramAreasSqFt": [facet["areaLabelSqFt"] for facet in facets if facet not in usable],
        "areaValidationTolerancePercent": area_tolerance_percent,
        "validationWarnings": inference_warnings,
        "unassignedFacetIds": [facet["id"] for facet in facets if facet not in usable],
    }


def _resolve_facet_geometry(
    facets: Sequence[dict[str, Any]], *, snap_tolerance_points: float
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fill missing pitch/direction data with explicit review-only estimates."""
    resolved: dict[str, dict[str, Any]] = {}
    for facet in facets:
        pitch = facet.get("pitch")
        direction = facet.get("downslopeDirectionPage")
        resolved[str(facet["id"])] = {
            "pitch": dict(pitch) if isinstance(pitch, dict) else None,
            "directionPage": list(direction) if direction else None,
            "pitchProvenance": "reported" if isinstance(pitch, dict) else "inferred",
            "directionProvenance": "reported" if direction else "inferred",
        }

    reported_pitches = [
        item["pitch"]
        for item in resolved.values()
        if item["pitch"]
    ]
    if not reported_pitches:
        return {}, ["No measured pitch is available to estimate unresolved facets."]
    known_pitches = [
        pitch for pitch in reported_pitches if float(pitch.get("rise", 0)) > 0
    ] or reported_pitches
    pitch_counts = Counter(
        (float(pitch["rise"]), float(pitch["run"]), str(pitch.get("notation", "")))
        for pitch in known_pitches
    )
    predominant_key = pitch_counts.most_common(1)[0][0]
    predominant_pitch = {
        "notation": predominant_key[2] or f"{predominant_key[0]:g}:{predominant_key[1]:g}",
        "rise": predominant_key[0],
        "run": predominant_key[1],
        "degrees": math.degrees(math.atan2(predominant_key[0], predominant_key[1])),
    }

    # Resolve missing pitch from the strongest shared boundary, then the nearest
    # containing outline. This also recovers very small facets whose pitch label
    # was not assigned because it fell on top of a neighboring vector outline.
    for _ in range(len(facets)):
        changed = False
        for facet in facets:
            current = resolved[str(facet["id"])]
            if current["pitch"] is not None:
                continue
            neighbors = _rank_facet_neighbors(facet, facets, snap_tolerance_points)
            source = next(
                (
                    neighbor
                    for _, neighbor, _ in neighbors
                    if resolved[str(neighbor["id"])]["pitch"] is not None
                ),
                None,
            )
            current["pitch"] = (
                dict(resolved[str(source["id"])]["pitch"])
                if source
                else dict(predominant_pitch)
            )
            current["pitchProvenance"] = "inferred"
            changed = True
        if not changed:
            break

    # A flat facet has no meaningful downslope vector. Give it a stable vector
    # for the schema while retaining its measured zero pitch.
    for facet in facets:
        current = resolved[str(facet["id"])]
        if current["pitch"] and float(current["pitch"].get("rise", 0)) == 0:
            if current["directionPage"] is None:
                current["directionPage"] = [1.0, 0.0]
                current["directionProvenance"] = "inferred"

    # For pitched facets, mirror a neighboring measured downslope across their
    # shared edge. On a common ridge this preserves the roof pitch and flips the
    # fall direction onto the unresolved face.
    for _ in range(len(facets)):
        changed = False
        for facet in facets:
            current = resolved[str(facet["id"])]
            if current["directionPage"] is not None:
                continue
            neighbors = _rank_facet_neighbors(facet, facets, snap_tolerance_points)
            selected = next(
                (
                    (shared_length, neighbor, tangent)
                    for shared_length, neighbor, tangent in neighbors
                    if resolved[str(neighbor["id"])]["directionPage"] is not None
                    and resolved[str(neighbor["id"])]["pitch"]
                    and float(resolved[str(neighbor["id"])]["pitch"].get("rise", 0)) > 0
                ),
                None,
            )
            if selected:
                shared_length, neighbor, tangent = selected
                source_direction = resolved[str(neighbor["id"])]["directionPage"]
                if shared_length > 0 and tangent is not None:
                    dot = source_direction[0] * tangent[0] + source_direction[1] * tangent[1]
                    direction = [
                        2 * dot * tangent[0] - source_direction[0],
                        2 * dot * tangent[1] - source_direction[1],
                    ]
                    magnitude = math.hypot(*direction)
                    current["directionPage"] = [direction[0] / magnitude, direction[1] / magnitude]
                else:
                    current["directionPage"] = list(source_direction)
                changed = True
        if not changed:
            break

    # Remaining isolated candidates keep the nearest measured direction. These
    # cases are called out below so they remain visibly review-only.
    for facet in facets:
        current = resolved[str(facet["id"])]
        if current["directionPage"] is not None:
            continue
        candidates = [
            other
            for other in facets
            if other["id"] != facet["id"]
            and resolved[str(other["id"])]["directionPage"] is not None
            and resolved[str(other["id"])]["pitch"]
            and float(resolved[str(other["id"])]["pitch"].get("rise", 0)) > 0
        ]
        if candidates:
            nearest = min(
                candidates,
                key=lambda other: _outline_distance(facet, other),
            )
            current["directionPage"] = list(resolved[str(nearest["id"])]["directionPage"])
        else:
            current["directionPage"] = [0.0, 1.0]
        current["directionProvenance"] = "inferred"

    inferred_pitch = [
        facet["id"]
        for facet in facets
        if resolved[str(facet["id"])]["pitchProvenance"] == "inferred"
    ]
    inferred_direction = [
        facet["id"]
        for facet in facets
        if resolved[str(facet["id"])]["directionProvenance"] == "inferred"
        and float(resolved[str(facet["id"])]["pitch"].get("rise", 0)) > 0
    ]
    warnings: list[str] = []
    if inferred_pitch:
        warnings.append(
            "Pitch is estimated for review on facets: " + ", ".join(inferred_pitch) + "."
        )
    if inferred_direction:
        warnings.append(
            "Downslope direction is estimated for review on facets: "
            + ", ".join(inferred_direction)
            + "."
        )
    return resolved, warnings


def _rank_facet_neighbors(
    facet: dict[str, Any],
    facets: Sequence[dict[str, Any]],
    tolerance: float,
) -> list[tuple[float, dict[str, Any], Point2 | None]]:
    neighbors = []
    for other in facets:
        if other["id"] == facet["id"]:
            continue
        shared_length, tangent = _shared_outline_edge(facet, other, tolerance)
        neighbors.append((shared_length, other, tangent))
    return sorted(
        neighbors,
        key=lambda item: (
            0 if item[0] > 0 else 1,
            -item[0],
            _outline_distance(facet, item[1]),
            str(item[1]["id"]),
        ),
    )


def _shared_outline_edge(
    first: dict[str, Any], second: dict[str, Any], tolerance: float
) -> tuple[float, Point2 | None]:
    first_points = [tuple(point) for point in first["polygonPagePoints"]]
    second_points = [tuple(point) for point in second["polygonPagePoints"]]
    best_length = 0.0
    best_tangent: Point2 | None = None
    for start, end in zip(first_points, first_points[1:] + first_points[:1], strict=True):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 1e-8:
            continue
        tangent = (dx / length, dy / length)
        for other_start, other_end in zip(
            second_points, second_points[1:] + second_points[:1], strict=True
        ):
            ox, oy = other_end[0] - other_start[0], other_end[1] - other_start[1]
            other_length = math.hypot(ox, oy)
            if other_length <= 1e-8 or abs(dx * oy - dy * ox) / (length * other_length) > 0.02:
                continue
            if max(
                abs((other_start[0] - start[0]) * dy - (other_start[1] - start[1]) * dx) / length,
                abs((other_end[0] - start[0]) * dy - (other_end[1] - start[1]) * dx) / length,
            ) > max(tolerance, 1.0):
                continue
            first_projection = (other_start[0] - start[0]) * tangent[0] + (
                other_start[1] - start[1]
            ) * tangent[1]
            second_projection = (other_end[0] - start[0]) * tangent[0] + (
                other_end[1] - start[1]
            ) * tangent[1]
            overlap = max(
                0.0,
                min(length, max(first_projection, second_projection))
                - max(0.0, min(first_projection, second_projection)),
            )
            if overlap > best_length:
                best_length = overlap
                best_tangent = tangent
    return best_length, best_tangent


def _outline_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_points = [tuple(point) for point in first["polygonPagePoints"]]
    second_points = [tuple(point) for point in second["polygonPagePoints"]]
    if point_in_polygon(polygon_centroid(first_points), second_points) or point_in_polygon(
        polygon_centroid(second_points), first_points
    ):
        return 0.0
    distances = [
        segment_distance(point, second_points[index], second_points[(index + 1) % len(second_points)])
        for point in first_points
        for index in range(len(second_points))
    ]
    distances.extend(
        segment_distance(point, first_points[index], first_points[(index + 1) % len(first_points)])
        for point in second_points
        for index in range(len(first_points))
    )
    return min(distances, default=float("inf"))


def _polygon_source_draw_order(page: VectorPage, target: Sequence[Point2]) -> int | None:
    """Find the content-stream path that closed a labeled outline, if one exists."""
    target_key = _polygon_signature(target, 0.8)
    for drawing_index, drawing in enumerate(page.drawings):
        for polygon in polygon_from_drawing(
            drawing, page_number=page.page_number, snap_tolerance=0.8
        ):
            if _polygon_signature(polygon, 0.8) == target_key:
                return drawing_index
    return None


def _polygon_signature(points: Sequence[Point2], tolerance: float) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((round(x / tolerance), round(y / tolerance)) for x, y in points))


def _overlap_order(planes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record only visible nested overlaps whose relative draw order comes from the PDF."""
    relations: list[dict[str, Any]] = []
    for first_index, first in enumerate(planes):
        first_polygon = [tuple(point) for point in first["vertices"]]
        first_order = first.get("sourceDrawOrder")
        if not isinstance(first_order, int):
            continue
        for second in planes[first_index + 1 :]:
            second_order = second.get("sourceDrawOrder")
            if not isinstance(second_order, int) or second_order == first_order:
                continue
            second_polygon = [tuple(point) for point in second["vertices"]]
            first_center = polygon_centroid(first_polygon)
            second_center = polygon_centroid(second_polygon)
            witness: Point2 | None = None
            if point_in_polygon(first_center, second_polygon) and not point_in_polygon(
                second_center, first_polygon
            ):
                witness = first_center
            elif point_in_polygon(second_center, first_polygon) and not point_in_polygon(
                first_center, second_polygon
            ):
                witness = second_center
            if witness is None:
                continue
            above, below = (first, second) if first_order > second_order else (second, first)
            relations.append(
                {
                    "abovePlaneId": above["id"],
                    "belowPlaneId": below["id"],
                    "witnessPlan": [round(witness[0], 6), round(witness[1], 6)],
                    "reason": "PDF content stream order for nested facet outlines",
                }
            )
    return relations


def _page_observation(page: VectorPage) -> dict[str, Any]:
    diagram = diagram_segments(page, minimum_length=1)
    return {
        "pageNumber": page.page_number,
        "title": page.title,
        "vectorSegmentCount": len(page.segments),
        "diagramSegmentCount": len(diagram),
        "numericDiagramLabels": [word.text for word in numeric_words(page)],
        "colorLegend": page.color_legend,
        "imageCount": page.image_count,
    }
