"""Server-side rebuilding and approval checks for versioned geometry edits."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.geometry.heights import solve_plane_heights
from app.geometry.topology import Point2, build_topology, point_in_polygon, polygon_centroid


def build_review_geometry(
    facets: Sequence[Mapping[str, Any]],
    *,
    feet_per_point: float | None,
    origin_page_point: Point2,
    height_tolerance_feet: float,
    source: str,
    confidence: str,
    snap_tolerance_points: float = 0.8,
    edge_classifier: Callable[[Point2, Point2], str] | None = None,
    report_name: str,
    area_tolerance_percent: float = 5.0,
    wall_depth_feet: float = 8.0,
    stacking_order_confirmed: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Rebuild topology and relative heights from edited PDF-page facet outlines.

    The caller supplies a calibrated page scale. Missing scale, pitch, or downslope
    directions leave the candidate geometry absent; no fallback dimensions are used.
    """
    diagnostics: dict[str, Any] = {"warnings": [], "failures": []}
    if feet_per_point is None or not math.isfinite(feet_per_point) or feet_per_point <= 0:
        diagnostics["warnings"].append(
            "A calibrated PDF-point-to-feet scale is required to build 3D geometry."
        )
        return None, diagnostics
    plan_polygons: list[dict[str, Any]] = []
    page_origin_x, page_origin_y = origin_page_point
    for facet in facets:
        identifier = str(facet.get("id", ""))
        page_points = facet.get("polygonPagePoints")
        if (
            not identifier
            or not isinstance(page_points, Sequence)
            or isinstance(page_points, (str, bytes))
            or len(page_points) < 3
        ):
            diagnostics["failures"].append(
                f"Facet {identifier or '(unnamed)'} needs at least three page-coordinate vertices."
            )
            continue
        pitch = facet.get("pitch")
        direction = facet.get("downslopeDirectionPage")
        if (
            not isinstance(pitch, Mapping)
            or not isinstance(direction, Sequence)
            or isinstance(direction, (str, bytes))
            or len(direction) != 2
        ):
            diagnostics["warnings"].append(
                f"Facet {identifier} has no assigned pitch and downslope direction."
            )
            continue
        rise = float(pitch.get("rise", 0))
        run = float(pitch.get("run", 0))
        direction_length = math.hypot(float(direction[0]), float(direction[1]))
        if rise < 0 or run <= 0 or direction_length <= 1e-8:
            diagnostics["failures"].append(
                f"Facet {identifier} has invalid pitch or direction values."
            )
            continue
        plan_points = [
            [
                (float(point[0]) - page_origin_x) * feet_per_point,
                (page_origin_y - float(point[1])) * feet_per_point,
            ]
            for point in page_points
        ]
        if any(not math.isfinite(value) for point in plan_points for value in point):
            diagnostics["failures"].append(f"Facet {identifier} contains non-finite vertices.")
            continue
        plan_direction = (
            float(direction[0]) / direction_length,
            -float(direction[1]) / direction_length,
        )
        report_area = facet.get("areaLabelSqFt", facet.get("reportedAreaSqFt"))
        edge_types = facet.get("edgeTypes", [])
        raw_confidence = facet.get("confidence", 0.75)
        if isinstance(raw_confidence, str):
            raw_confidence = {"high": 0.95, "medium": 0.75, "low": 0.5}.get(
                raw_confidence, 0.5
            )
        try:
            confidence = min(max(float(raw_confidence), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.5
        plan_polygons.append(
            {
                "id": identifier,
                "name": str(facet.get("name", identifier)),
                "roofType": str(facet.get("roofType", "pitched")),
                "vertices": plan_points,
                "reportedAreaSqFt": float(report_area) if report_area is not None else None,
                "pitch": {
                    "notation": str(pitch.get("notation", f"{rise:g}:{run:g}")),
                    "rise": rise,
                    "run": run,
                },
                "downslopePlanUnitVector": plan_direction,
                "drawOrder": int(facet.get("drawOrder", 0)),
                "provenance": facet.get("provenance", {
                    "outline": "reported",
                    "pitch": "reported",
                    "downslopeDirection": "reported",
                    "area": "reported",
                }),
                "confidence": confidence,
                "edgeTypes": list(edge_types)
                if isinstance(edge_types, Sequence) and not isinstance(edge_types, (str, bytes))
                else [],
                "pagePoints": [[float(point[0]), float(point[1])] for point in page_points],
            }
        )
    if diagnostics["failures"]:
        return None, diagnostics
    if not plan_polygons:
        diagnostics["warnings"].append(
            "No facets have enough pitch and direction evidence to form a 3D candidate."
        )
        return None, diagnostics

    explicit_edge_types: dict[tuple[tuple[int, int], tuple[int, int]], str] = {}
    for polygon in plan_polygons:
        page_points = polygon["pagePoints"]
        edge_types = polygon["edgeTypes"]
        if len(edge_types) != len(page_points):
            continue
        for index, first in enumerate(page_points):
            second = page_points[(index + 1) % len(page_points)]
            key = _edge_key(first, second)
            previous = explicit_edge_types.get(key)
            current = str(edge_types[index])
            if previous is None or _edge_priority(current) > _edge_priority(previous):
                explicit_edge_types[key] = current

    def classify_edge(start: Point2, end: Point2) -> str:
        first_page = (
            start[0] / feet_per_point + page_origin_x,
            page_origin_y - start[1] / feet_per_point,
        )
        second_page = (
            end[0] / feet_per_point + page_origin_x,
            page_origin_y - end[1] / feet_per_point,
        )
        explicit = explicit_edge_types.get(_edge_key(first_page, second_page))
        return explicit or (edge_classifier(start, end) if edge_classifier else "other")

    try:
        topology = build_topology(
            plan_polygons,
            snap_tolerance=max(feet_per_point * snap_tolerance_points, 0.005),
            edge_classifier=classify_edge,
        )
    except (KeyError, TypeError, ValueError) as exc:
        diagnostics["failures"].append(f"Topology rebuild failed: {type(exc).__name__}.")
        return None, diagnostics
    eave_edges = {edge["id"] for edge in topology["edges"] if edge["type"] == "eave"}
    overlap_order, unresolved_overlaps = _find_overlap_order(
        plan_polygons, confirmed=stacking_order_confirmed
    )
    height_result = solve_plane_heights(
        {
            "vertices": topology["vertices"],
            "planes": topology["planes"],
            "edges": topology["edges"],
        },
        eave_edge_ids=eave_edges,
        overlap_order=overlap_order,
        tolerance_feet=height_tolerance_feet,
    )
    # Do not emit averaged/compromise heights when two source constraints conflict.
    # The version remains reviewable, but its 3D data cannot be approved.
    if height_result["status"] == "solved":
        for vertex in topology["vertices"]:
            height = height_result["vertexHeights"].get(vertex["id"])
            if height is not None:
                vertex["plan"][2] = height
        vertices_by_id = {vertex["id"]: vertex["plan"] for vertex in topology["vertices"]}
        for edge in topology["edges"]:
            first, second = edge["vertexIds"]
            edge["lengthFt"] = math.dist(vertices_by_id[first], vertices_by_id[second])
    diagnostics["warnings"].extend(height_result["warnings"])
    diagnostics["failures"].extend(height_result["failures"])
    diagnostics["unresolvedOverlaps"] = unresolved_overlaps
    if unresolved_overlaps:
        diagnostics["warnings"].append(
            "Intersecting facets need an explicitly reviewed stacking order before approval."
        )
    plan_vertices = {vertex["id"]: vertex["plan"] for vertex in topology["vertices"]}
    outer_edges = [edge for edge in topology["edges"] if len(edge["adjacentPlaneIds"]) == 1]
    wall_loops, wall_warnings = _ordered_boundary_loops(
        outer_edges, ground_plane_height_ft=-wall_depth_feet
    )
    diagnostics["warnings"].extend(wall_warnings)
    plan_area_total = sum(float(plane["planAreaSqFt"]) for plane in topology["planes"])
    mesh_area_total = sum(float(plane["meshSurfaceAreaSqFt"]) for plane in topology["planes"])
    geometry = {
        "schemaVersion": "1.2",
        "geometryId": f"{source}-review-candidate-v1",
        "status": "needs_review",
        "source": source,
        "confidence": confidence,
        "estimatedHeights": True,
        "units": "feet",
        "coordinateSystem": {
            "planAxes": "u is right and v is up on the source report diagram",
            "heightAxis": "relative to the lowest detected eave; absolute elevation unavailable",
            "threeJsMapping": "X=u, Y=h, Z=-v",
            "orientation": "Preserves source report diagram orientation; no north-up rotation",
        },
        "compass": {
            "north": "source diagram",
            "east": "source diagram",
            "south": "source diagram",
            "west": "source diagram",
            "source": report_name,
        },
        "sourceReference": {
            "viewer": "",
            "report": report_name,
            "method": (
                "PyMuPDF vector outlines; contractor edits; server-side topology "
                "and height recomputation"
            ),
        },
        "heightModel": {
            "formula": "h = plane offset - (rise/run) * dot(plan, downslope direction)",
            "manualHeightOverrides": [],
            "flatDrainPitch": {
                "rise": 1,
                "run": 12,
                "direction": "Per assigned report arrow or contractor correction",
            },
            "wallGroundPlaneHeightFt": -wall_depth_feet,
            "wallGroundPlaneIsEstimated": True,
        },
        "vertices": topology["vertices"],
        "planes": topology["planes"],
        "edges": topology["edges"],
        "wallLoops": wall_loops,
        "overlapOrder": overlap_order,
        "omittedDiagramAreasSqFt": [],
        "areaValidationTolerancePercent": area_tolerance_percent,
        "validationWarnings": list(
            dict.fromkeys(diagnostics["warnings"] + diagnostics["failures"])
        ),
        "heightSolve": height_result,
        "summary": {
            "planAreaSqFt": round(plan_area_total, 3),
            "surfaceAreaSqFt": round(mesh_area_total, 3),
        },
    }
    diagnostics["heightSolve"] = height_result
    diagnostics["vertexCount"] = len(plan_vertices)
    diagnostics["planeCount"] = len(topology["planes"])
    diagnostics["edgeCount"] = len(topology["edges"])
    return geometry, diagnostics


def _find_overlap_order(
    planes: Sequence[Mapping[str, Any]], *, confirmed: bool
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    relations: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for first_index, first in enumerate(planes):
        first_polygon = [tuple(point) for point in first["vertices"]]
        for second in planes[first_index + 1 :]:
            second_polygon = [tuple(point) for point in second["vertices"]]
            witness = _overlap_witness(first_polygon, second_polygon)
            if witness is None:
                continue
            first_id = str(first["id"])
            second_id = str(second["id"])
            if not confirmed:
                unresolved.append({"firstPlaneId": first_id, "secondPlaneId": second_id})
                continue
            if int(first.get("drawOrder", 0)) == int(second.get("drawOrder", 0)):
                unresolved.append({"firstPlaneId": first_id, "secondPlaneId": second_id})
                continue
            above, below = (
                (first, second)
                if int(first.get("drawOrder", 0)) > int(second.get("drawOrder", 0))
                else (second, first)
            )
            relations.append(
                {
                    "abovePlaneId": str(above["id"]),
                    "belowPlaneId": str(below["id"]),
                    "witnessPlan": [round(witness[0], 6), round(witness[1], 6)],
                    "reason": "Contractor-confirmed stacking order for overlapping traced facets",
                }
            )
    return relations, unresolved


def _overlap_witness(first: Sequence[Point2], second: Sequence[Point2]) -> Point2 | None:
    def strictly_inside(point: Point2, polygon: Sequence[Point2]) -> bool:
        if not point_in_polygon(point, polygon):
            return False
        from app.geometry.topology import segment_distance

        return (
            min(
                segment_distance(point, polygon[index], polygon[(index + 1) % len(polygon)])
                for index in range(len(polygon))
            )
            > 1e-5
        )

    first_center = polygon_centroid(first)
    second_center = polygon_centroid(second)
    if strictly_inside(first_center, second):
        return first_center
    if strictly_inside(second_center, first):
        return second_center
    for point in first:
        if strictly_inside(point, second):
            return point
    for point in second:
        if strictly_inside(point, first):
            return point
    return None


def _ordered_boundary_loops(
    edges: Sequence[Mapping[str, Any]], *, ground_plane_height_ft: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return only closed, degree-two boundary cycles; never invent a closing wall."""
    boundary = [edge for edge in edges if len(edge.get("adjacentPlaneIds", [])) == 1]
    incident: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for edge in boundary:
        vertex_ids = edge.get("vertexIds", [])
        if len(vertex_ids) != 2:
            continue
        first, second = str(vertex_ids[0]), str(vertex_ids[1])
        incident.setdefault(first, []).append((second, edge))
        incident.setdefault(second, []).append((first, edge))
    if not incident:
        return [], ["No closed roof boundary was available for estimated wall surfaces."]

    unseen = set(incident)
    loops: list[dict[str, Any]] = []
    warnings: list[str] = []
    while unseen:
        start = min(unseen)
        component: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(neighbor for neighbor, _ in incident.get(current, []))
        unseen.difference_update(component)
        if any(len(incident.get(vertex, [])) != 2 for vertex in component):
            warnings.append(
                "Some roof boundary edges do not form a closed simple loop; "
                "estimated walls were omitted for that boundary."
            )
            continue
        ordered = [start]
        previous: str | None = None
        current = start
        for _ in range(len(component) + 1):
            choices = [neighbor for neighbor, _ in incident[current] if neighbor != previous]
            if not choices:
                break
            next_vertex = min(choices) if previous is None else choices[0]
            if next_vertex == start:
                break
            if next_vertex in ordered:
                ordered = []
                break
            ordered.append(next_vertex)
            previous, current = current, next_vertex
        if len(ordered) != len(component):
            warnings.append(
                "A roof boundary could not be ordered as one simple loop; "
                "estimated walls were omitted for that boundary."
            )
            continue
        loops.append(
            {
                "id": f"estimated-outer-walls-{len(loops) + 1:02d}",
                "label": "Estimated walls",
                "boundaryVertexIds": ordered,
                "groundPlaneHeightFt": ground_plane_height_ft,
                "estimated": True,
            }
        )
    return loops, list(dict.fromkeys(warnings))


def approval_blockers(analysis: Mapping[str, Any], geometry: Mapping[str, Any] | None) -> list[str]:
    blockers: list[str] = []
    facets = analysis.get("facets", [])
    if not isinstance(facets, Sequence) or not facets:
        blockers.append("No source facets are available to approve.")
    else:
        for facet in facets:
            if not isinstance(facet, Mapping):
                blockers.append("A plane record is invalid.")
                continue
            if not isinstance(facet.get("pitch"), Mapping):
                blockers.append(f"{facet.get('id', 'A plane')} has no pitch.")
            if not isinstance(facet.get("downslopeDirectionPage"), Sequence):
                blockers.append(f"{facet.get('id', 'A plane')} has no downslope direction.")
    if geometry is None:
        blockers.append("A 3D geometry candidate could not be built from calibrated source data.")
    else:
        solve = geometry.get("heightSolve")
        if not isinstance(solve, Mapping) or solve.get("status") != "solved":
            blockers.append("Height constraints are incomplete or contradictory.")
    validation = analysis.get("validation")
    build_diagnostics = (
        validation.get("buildDiagnostics") if isinstance(validation, Mapping) else None
    )
    unresolved = (
        build_diagnostics.get("unresolvedOverlaps", [])
        if isinstance(build_diagnostics, Mapping)
        else []
    )
    if unresolved:
        blockers.append("Intersecting planes need a reviewed, non-ambiguous stacking order.")
    checks = validation.get("checks", []) if isinstance(validation, Mapping) else []
    for check in checks:
        if isinstance(check, Mapping) and check.get("status") in {"fail", "warn", "not_evaluable"}:
            blockers.append(
                f"Validation check {check.get('name', 'unknown')} is {check.get('status')}."
            )
    return list(dict.fromkeys(blockers))


def _edge_key(
    first: Sequence[float], second: Sequence[float]
) -> tuple[tuple[int, int], tuple[int, int]]:
    a = (round(float(first[0]) * 100), round(float(first[1]) * 100))
    b = (round(float(second[0]) * 100), round(float(second[1]) * 100))
    return (a, b) if a <= b else (b, a)


def _edge_priority(edge_type: str) -> int:
    return {
        "other": 0,
        "eave": 1,
        "rake": 1,
        "transition": 2,
        "ridge": 3,
        "hip": 3,
        "valley": 4,
    }.get(edge_type, 0)
