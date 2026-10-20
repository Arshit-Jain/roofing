"""Planar graph polygonization and shared-edge topology helpers."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeAlias

Point2: TypeAlias = tuple[float, float]
Segment2: TypeAlias = tuple[Point2, Point2]


def polygon_area(points: Sequence[Point2]) -> float:
    if len(points) < 3:
        return 0.0
    return (
        abs(
            sum(
                points[index][0] * points[(index + 1) % len(points)][1]
                - points[(index + 1) % len(points)][0] * points[index][1]
                for index in range(len(points))
            )
        )
        / 2
    )


def signed_polygon_area(points: Sequence[Point2]) -> float:
    return (
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
        / 2
    )


def point_in_polygon(point: Point2, polygon: Sequence[Point2], *, tolerance: float = 1e-7) -> bool:
    """Return true for interior or boundary points using a ray crossing test."""
    x, y = point
    inside = False
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        cross = (x - start[0]) * (end[1] - start[1]) - (y - start[1]) * (end[0] - start[0])
        dot = (x - start[0]) * (end[0] - start[0]) + (y - start[1]) * (end[1] - start[1])
        length_squared = (end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2
        if abs(cross) <= tolerance and -tolerance <= dot <= length_squared + tolerance:
            return True
        if (start[1] > y) != (end[1] > y):
            intersection_x = start[0] + (y - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
            if x < intersection_x:
                inside = not inside
    return inside


def polygon_centroid(points: Sequence[Point2]) -> Point2:
    signed_area = signed_polygon_area(points)
    if abs(signed_area) < 1e-12:
        return (
            sum(point[0] for point in points) / max(len(points), 1),
            sum(point[1] for point in points) / max(len(points), 1),
        )
    x_sum = 0.0
    y_sum = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        cross = point[0] * next_point[1] - next_point[0] * point[1]
        x_sum += (point[0] + next_point[0]) * cross
        y_sum += (point[1] + next_point[1]) * cross
    return x_sum / (6 * signed_area), y_sum / (6 * signed_area)


def segment_distance(point: Point2, start: Point2, end: Point2) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return math.dist(point, start)
    fraction = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator),
    )
    return math.hypot(point[0] - start[0] - fraction * dx, point[1] - start[1] - fraction * dy)


def split_segments_at_intersections(
    segments: Iterable[Segment2], *, parameter_tolerance: float = 1e-8
) -> list[Segment2]:
    """Split vector strokes at crossings and at endpoints that land on another stroke."""
    source = [
        (start, end)
        for start, end in segments
        if math.dist(start, end) > 1e-9
    ]
    cuts: list[list[float]] = [[0.0, 1.0] for _ in source]

    def cross(first: Point2, second: Point2) -> float:
        return first[0] * second[1] - first[1] * second[0]

    for first_index, (first_start, first_end) in enumerate(source):
        first_vector = (
            first_end[0] - first_start[0],
            first_end[1] - first_start[1],
        )
        first_length_squared = (
            first_vector[0] * first_vector[0] + first_vector[1] * first_vector[1]
        )
        first_bounds = (
            min(first_start[0], first_end[0]),
            min(first_start[1], first_end[1]),
            max(first_start[0], first_end[0]),
            max(first_start[1], first_end[1]),
        )
        for second_index in range(first_index + 1, len(source)):
            second_start, second_end = source[second_index]
            if (
                max(second_start[0], second_end[0]) < first_bounds[0] - parameter_tolerance
                or max(second_start[1], second_end[1]) < first_bounds[1] - parameter_tolerance
                or min(second_start[0], second_end[0]) > first_bounds[2] + parameter_tolerance
                or min(second_start[1], second_end[1]) > first_bounds[3] + parameter_tolerance
            ):
                continue
            second_vector = (
                second_end[0] - second_start[0],
                second_end[1] - second_start[1],
            )
            second_length_squared = (
                second_vector[0] * second_vector[0] + second_vector[1] * second_vector[1]
            )
            if second_length_squared <= 1e-18:
                continue

            offset = (
                second_start[0] - first_start[0],
                second_start[1] - first_start[1],
            )
            denominator = cross(first_vector, second_vector)
            scale = max(
                1.0,
                math.sqrt(first_length_squared * second_length_squared),
            )
            if abs(denominator) > parameter_tolerance * scale:
                first_fraction = cross(offset, second_vector) / denominator
                second_fraction = cross(offset, first_vector) / denominator
                if (
                    -parameter_tolerance <= first_fraction <= 1 + parameter_tolerance
                    and -parameter_tolerance <= second_fraction <= 1 + parameter_tolerance
                ):
                    cuts[first_index].append(min(1.0, max(0.0, first_fraction)))
                    cuts[second_index].append(min(1.0, max(0.0, second_fraction)))
                continue

            if abs(cross(offset, first_vector)) > parameter_tolerance * max(
                1.0, math.sqrt(first_length_squared)
            ):
                continue

            # Collinear strokes can overlap only partially. Split each stroke at
            # the other stroke's endpoints so the planar walk sees every junction.
            for point in (second_start, second_end):
                fraction = (
                    (point[0] - first_start[0]) * first_vector[0]
                    + (point[1] - first_start[1]) * first_vector[1]
                ) / first_length_squared
                if -parameter_tolerance <= fraction <= 1 + parameter_tolerance:
                    cuts[first_index].append(min(1.0, max(0.0, fraction)))
            for point in (first_start, first_end):
                fraction = (
                    (point[0] - second_start[0]) * second_vector[0]
                    + (point[1] - second_start[1]) * second_vector[1]
                ) / second_length_squared
                if -parameter_tolerance <= fraction <= 1 + parameter_tolerance:
                    cuts[second_index].append(min(1.0, max(0.0, fraction)))

    result: list[Segment2] = []
    for (start, end), fractions in zip(source, cuts, strict=True):
        dx, dy = end[0] - start[0], end[1] - start[1]
        ordered = sorted(set(round(fraction, 10) for fraction in fractions))
        for first, second in zip(ordered, ordered[1:], strict=False):
            if second - first <= parameter_tolerance:
                continue
            result.append(
                (
                    (start[0] + dx * first, start[1] + dy * first),
                    (start[0] + dx * second, start[1] + dy * second),
                )
            )
    return result


class _SnapIndex:
    def __init__(self, tolerance: float) -> None:
        if tolerance <= 0:
            raise ValueError("Snap tolerance must be positive")
        self.tolerance = tolerance
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.points: list[Point2] = []

    def add(self, point: Point2) -> int:
        cell = (round(point[0] / self.tolerance), round(point[1] / self.tolerance))
        for x in range(cell[0] - 1, cell[0] + 2):
            for y in range(cell[1] - 1, cell[1] + 2):
                for identifier in self.cells.get((x, y), []):
                    if math.dist(self.points[identifier], point) <= self.tolerance:
                        return identifier
        identifier = len(self.points)
        self.points.append(point)
        self.cells[cell].append(identifier)
        return identifier


def polygonize_segments(
    segments: Iterable[Segment2], *, snap_tolerance: float = 0.8, minimum_area: float = 1.0
) -> tuple[list[list[Point2]], list[dict[str, Any]]]:
    """Polygonize a snapped vector line network with a half-edge face walk.

    Open components and large unclosed gaps are returned as diagnostics. The function
    never adds closure edges to incomplete outlines.
    """
    index = _SnapIndex(snap_tolerance)
    graph: dict[int, set[int]] = defaultdict(set)
    edge_sources: dict[tuple[int, int], int] = defaultdict(int)
    for start, end in segments:
        if math.dist(start, end) <= 1e-9:
            continue
        first = index.add(start)
        second = index.add(end)
        if first == second:
            continue
        key = (min(first, second), max(first, second))
        edge_sources[key] += 1
        graph[first].add(second)
        graph[second].add(first)

    unclosed = [identifier for identifier, neighbors in graph.items() if len(neighbors) == 1]
    diagnostics: list[dict[str, Any]] = []
    if unclosed:
        endpoints = sorted(
            (index.points[identifier] for identifier in unclosed),
            key=lambda point: (point[1], point[0]),
        )
        diagnostics.append(
            {
                "code": "unclosed_outline_gaps",
                "message": "Vector outlines contain open endpoints; no closure was inferred.",
                "endpointCount": len(endpoints),
                "endpoints": [[round(point[0], 3), round(point[1], 3)] for point in endpoints[:40]],
            }
        )

    angle_neighbors = {
        vertex: sorted(
            neighbors,
            key=lambda neighbor: math.atan2(
                index.points[neighbor][1] - index.points[vertex][1],
                index.points[neighbor][0] - index.points[vertex][0],
            ),
        )
        for vertex, neighbors in graph.items()
    }
    visited: set[tuple[int, int]] = set()
    faces: list[list[Point2]] = []
    maximum_steps = max(2 * len(edge_sources) + 1, 1)
    for first, second in sorted(
        ((a, b) for a, neighbors in graph.items() for b in neighbors),
        key=lambda pair: (pair[0], pair[1]),
    ):
        if (first, second) in visited:
            continue
        start_edge = (first, second)
        current = start_edge
        ring_ids: list[int] = []
        for _ in range(maximum_steps):
            if current in visited:
                break
            visited.add(current)
            origin, destination = current
            ring_ids.append(origin)
            neighbors = angle_neighbors[destination]
            try:
                reverse_index = neighbors.index(origin)
            except ValueError:
                break
            next_vertex = neighbors[(reverse_index - 1) % len(neighbors)]
            current = (destination, next_vertex)
            if current == start_edge:
                break
        if current != start_edge or len(ring_ids) < 3:
            continue
        ring = [index.points[identifier] for identifier in ring_ids]
        signed_area = signed_polygon_area(ring)
        if signed_area <= minimum_area:
            continue
        faces.append(_remove_collinear(ring, snap_tolerance * 0.05))

    unique: dict[tuple[tuple[int, int], ...], list[Point2]] = {}
    for face in faces:
        face_key = tuple(
            sorted((round(x / snap_tolerance), round(y / snap_tolerance)) for x, y in face)
        )
        unique[face_key] = face
    return sorted(
        unique.values(),
        key=lambda polygon: (min(p[1] for p in polygon), min(p[0] for p in polygon)),
    ), diagnostics


def _remove_collinear(points: Sequence[Point2], tolerance: float) -> list[Point2]:
    if len(points) < 4:
        return list(points)
    kept: list[Point2] = []
    for index, point in enumerate(points):
        previous = points[index - 1]
        following = points[(index + 1) % len(points)]
        cross = abs(
            (point[0] - previous[0]) * (following[1] - point[1])
            - (point[1] - previous[1]) * (following[0] - point[0])
        )
        if cross > tolerance:
            kept.append(point)
    return kept if len(kept) >= 3 else list(points)


def build_topology(
    planes: Sequence[dict[str, Any]],
    *,
    snap_tolerance: float = 0.01,
    edge_classifier: Callable[[Point2, Point2], str] | None = None,
) -> dict[str, Any]:
    """Build deterministic shared vertex and edge IDs from plane plan outlines."""
    prepared: list[tuple[dict[str, Any], str, list[Point2]]] = []
    all_outline_points: list[Point2] = []
    for plane in planes:
        plane_id = str(plane["id"])
        raw_vertices = plane.get("vertices")
        if not isinstance(raw_vertices, Sequence) or isinstance(raw_vertices, (str, bytes)):
            raise ValueError(f"Plane {plane_id} has no vertices")
        coords = [(float(point[0]), float(point[1])) for point in raw_vertices]
        if any(not math.isfinite(value) for point in coords for value in point):
            raise ValueError(f"Plane {plane_id} contains non-finite coordinates")
        if len(coords) > 1 and math.dist(coords[0], coords[-1]) <= snap_tolerance:
            coords.pop()
        if len(coords) < 3:
            raise ValueError(f"Plane {plane_id} needs at least three vertices")
        prepared.append((plane, plane_id, coords))
        all_outline_points.extend(coords)

    # A newly traced area can share only part of an existing edge. Insert its
    # endpoints into that edge so both planes resolve to the same topology edges.
    for index, (plane, plane_id, coords) in enumerate(prepared):
        split_coords: list[Point2] = []
        own_vertices = set(coords)
        for edge_index, start in enumerate(coords):
            end = coords[(edge_index + 1) % len(coords)]
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length_squared = dx * dx + dy * dy
            length = math.sqrt(length_squared)
            split_coords.append(start)
            if length <= 1e-9:
                continue
            interior: list[tuple[float, Point2]] = []
            for point in all_outline_points:
                # A vertex from this same outline may lie very close to another
                # edge without actually being on it. Re-inserting its projection
                # snaps the two coordinates together and creates a repeated
                # boundary vertex (a zero-width self-touch) in the output mesh.
                # Its original position is already present in this ring, so only
                # endpoints from other outlines need to split shared edges.
                if point in own_vertices:
                    continue
                fraction = (
                    (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
                ) / length_squared
                if fraction <= 1e-9 or fraction >= 1 - 1e-9:
                    continue
                if segment_distance(point, start, end) > snap_tolerance:
                    continue
                projected = (start[0] + fraction * dx, start[1] + fraction * dy)
                interior.append((fraction, projected))
            interior.sort(key=lambda item: item[0])
            previous_fraction = -1.0
            for fraction, projected in interior:
                if fraction - previous_fraction <= max(1e-9, snap_tolerance / length):
                    continue
                split_coords.append(projected)
                previous_fraction = fraction
        prepared[index] = (plane, plane_id, split_coords)

    snap_index = _SnapIndex(snap_tolerance)
    plane_vertices: dict[str, list[int]] = {}
    plane_areas: dict[str, float] = {}
    plane_input: dict[str, dict[str, Any]] = {}
    for plane, plane_id, coords in prepared:
        if not plane_id or plane_id in plane_input:
            raise ValueError("Plane IDs must be non-empty and unique")
        if _self_intersects(coords, snap_tolerance * 1e-6):
            raise ValueError(f"Plane {plane_id} has a self-intersecting boundary")
        identifiers = [snap_index.add(point) for point in coords]
        identifiers = [
            identifier
            for index, identifier in enumerate(identifiers)
            if index == 0 or identifier != identifiers[index - 1]
        ]
        if len(identifiers) > 1 and identifiers[0] == identifiers[-1]:
            identifiers.pop()
        if len(set(identifiers)) < 3:
            raise ValueError(f"Plane {plane_id} collapses after vertex snapping")
        plane_vertices[plane_id] = identifiers
        plane_areas[plane_id] = polygon_area([snap_index.points[value] for value in identifiers])
        if plane_areas[plane_id] <= snap_tolerance**2:
            raise ValueError(f"Plane {plane_id} has no measurable plan area")
        plane_input[plane_id] = dict(plane)

    vertex_ids = {identifier: f"v-{identifier:04d}" for identifier in range(len(snap_index.points))}
    edge_planes: dict[tuple[int, int], list[str]] = defaultdict(list)
    for plane_id, identifiers in plane_vertices.items():
        for index, first in enumerate(identifiers):
            second = identifiers[(index + 1) % len(identifiers)]
            key = (min(first, second), max(first, second))
            if plane_id not in edge_planes[key]:
                edge_planes[key].append(plane_id)

    edge_records: list[dict[str, Any]] = []
    plane_edge_ids: dict[str, list[str]] = {plane_id: [] for plane_id in plane_vertices}
    edge_keys: dict[tuple[int, int], str] = {}
    for edge_index, key in enumerate(sorted(edge_planes)):
        first, second = key
        first_point, second_point = snap_index.points[first], snap_index.points[second]
        edge_id = f"edge-{edge_index + 1:04d}"
        edge_keys[key] = edge_id
        edge_type = edge_classifier(first_point, second_point) if edge_classifier else "other"
        length = math.dist(first_point, second_point)
        adjacent = sorted(edge_planes[key])
        edge_records.append(
            {
                "id": edge_id,
                "vertexIds": [vertex_ids[first], vertex_ids[second]],
                "type": edge_type,
                "adjacentPlaneIds": adjacent,
                "planLengthFt": length,
                "lengthFt": length,
            }
        )
        if len(adjacent) > 2:
            raise ValueError(f"Edge {edge_id} is shared by more than two planes")
        for plane_id in adjacent:
            plane_edge_ids[plane_id].append(edge_id)

    vertex_records = [
        {"id": vertex_ids[index], "plan": [point[0], point[1], 0.0]}
        for index, point in enumerate(snap_index.points)
    ]
    plane_records = []
    for plane_id, identifiers in plane_vertices.items():
        plane = plane_input[plane_id]
        pitch_data = plane.get("pitch")
        if not isinstance(pitch_data, dict):
            raise ValueError(f"Plane {plane_id} has no resolved pitch")
        rise = float(pitch_data["rise"])
        run = float(pitch_data["run"])
        plan_area = plane_areas[plane_id]
        report_area = plane.get("reportedAreaSqFt")
        plane_records.append(
            {
                "id": plane_id,
                "name": str(plane.get("name", plane_id)),
                "roofType": plane.get("roofType", "pitched"),
                "boundaryVertexIds": [vertex_ids[identifier] for identifier in identifiers],
                "edgeIds": [
                    edge_keys[(min(first, second), max(first, second))]
                    for first, second in zip(
                        identifiers, identifiers[1:] + identifiers[:1], strict=True
                    )
                ],
                "pitch": {
                    "notation": str(pitch_data.get("notation", f"{rise:g}:{run:g}")),
                    "rise": rise,
                    "run": run,
                    "degrees": math.degrees(math.atan2(rise, run)),
                    "downslopePlanUnitVector": list(plane["downslopePlanUnitVector"]),
                },
                "reportedAreaSqFt": float(report_area) if report_area is not None else None,
                "planAreaSqFt": plan_area,
                "slopeAdjustedAreaSqFt": plan_area * math.sqrt(1 + (rise / run) ** 2),
                "meshSurfaceAreaSqFt": plan_area * math.sqrt(1 + (rise / run) ** 2),
                "textureRepeatFeet": 3,
                "drawOrder": int(plane.get("drawOrder", 0)),
                "provenance": dict(plane.get("provenance", {})),
                "confidence": min(max(float(plane.get("confidence", 0.5)), 0.0), 1.0),
            }
        )

    return {
        "vertices": vertex_records,
        "planes": plane_records,
        "edges": edge_records,
        "planeAreas": plane_areas,
        "boundaryVertexIndexes": plane_vertices,
    }


def _self_intersects(points: Sequence[Point2], tolerance: float) -> bool:
    """Reject bow-tie and overlapping boundaries before they become roof faces."""

    def orientation(a: Point2, b: Point2, c: Point2) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point2, b: Point2, p: Point2) -> bool:
        return (
            min(a[0], b[0]) - tolerance <= p[0] <= max(a[0], b[0]) + tolerance
            and min(a[1], b[1]) - tolerance <= p[1] <= max(a[1], b[1]) + tolerance
        )

    count = len(points)
    for first_index in range(count):
        first_start = points[first_index]
        first_end = points[(first_index + 1) % count]
        for second_index in range(first_index + 1, count):
            if second_index in {first_index, (first_index + 1) % count}:
                continue
            if first_index == 0 and second_index == count - 1:
                continue
            second_start = points[second_index]
            second_end = points[(second_index + 1) % count]
            o1 = orientation(first_start, first_end, second_start)
            o2 = orientation(first_start, first_end, second_end)
            o3 = orientation(second_start, second_end, first_start)
            o4 = orientation(second_start, second_end, first_end)
            if ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance)) and (
                (o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance)
            ):
                return True
            if abs(o1) <= tolerance and on_segment(first_start, first_end, second_start):
                return True
            if abs(o2) <= tolerance and on_segment(first_start, first_end, second_end):
                return True
            if abs(o3) <= tolerance and on_segment(second_start, second_end, first_start):
                return True
            if abs(o4) <= tolerance and on_segment(second_start, second_end, first_end):
                return True
    return False


def _interior_sample(polygon: Sequence[Point2]) -> Point2:
    centroid = polygon_centroid(polygon)
    if point_in_polygon(centroid, polygon):
        return centroid
    first = polygon[0]
    second = polygon[1]
    return ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
