"""Constraint-based facet height solving. Missing constraints never become invented roofs."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Any, Literal


def solve_plane_heights(
    topology: dict[str, Any],
    *,
    eave_edge_ids: set[str],
    overlap_order: Sequence[dict[str, Any]] = (),
    tolerance_feet: float = 0.05,
    overlap_gap_feet: float = 0.01,
) -> dict[str, Any]:
    """Solve per-plane offsets from pitch gradients and shared-edge equalities.

    An eave supplies the local zero datum. Shared edges supply plane-to-plane
    constraints. Overlap-only components need an explicit estimated offset; if a
    page ordering cannot determine that offset, the result remains needs_review.
    """
    vertices = {vertex["id"]: vertex["plan"] for vertex in topology.get("vertices", [])}
    planes = {plane["id"]: plane for plane in topology.get("planes", [])}
    if not planes:
        return _result("needs_review", {}, ["No polygon planes were recovered."], [], [])

    gradients: dict[str, tuple[float, float]] = {}
    for plane_id, plane in planes.items():
        pitch = plane.get("pitch")
        direction = pitch.get("downslopePlanUnitVector") if isinstance(pitch, dict) else None
        if (
            not isinstance(direction, Sequence)
            or len(direction) != 2
            or not isinstance(pitch, dict)
            or float(pitch.get("run", 0)) <= 0
        ):
            return _result(
                "needs_review",
                {},
                [f"Plane {plane_id} has no supported pitch and downslope direction."],
                [],
                [],
            )
        magnitude = math.hypot(float(direction[0]), float(direction[1]))
        if magnitude <= 1e-8:
            return _result(
                "needs_review", {}, [f"Plane {plane_id} has a zero downslope vector."], [], []
            )
        rise = float(pitch.get("rise", 0))
        run = float(pitch["run"])
        gradients[plane_id] = (
            -rise / run * float(direction[0]) / magnitude,
            -rise / run * float(direction[1]) / magnitude,
        )

    constraints: list[tuple[str, str, float, str]] = []
    warnings: list[str] = []
    failures: list[str] = []
    for edge in topology.get("edges", []):
        adjacent = edge.get("adjacentPlaneIds", [])
        if len(adjacent) != 2:
            continue
        first_id, second_id = str(adjacent[0]), str(adjacent[1])
        if first_id not in planes or second_id not in planes:
            failures.append(f"Shared edge {edge.get('id')} references a missing plane.")
            continue
        endpoint_ids = edge.get("vertexIds", [])
        points = [vertices.get(identifier) for identifier in endpoint_ids]
        if len(points) != 2 or any(point is None for point in points):
            failures.append(f"Shared edge {edge.get('id')} references a missing vertex.")
            continue
        first_gradient, second_gradient = gradients[first_id], gradients[second_id]
        deltas = [
            (first_gradient[0] - second_gradient[0]) * float(point[0])
            + (first_gradient[1] - second_gradient[1]) * float(point[1])
            for point in points
            if point is not None
        ]
        if len(deltas) != 2:
            continue
        mismatch = abs(deltas[0] - deltas[1])
        edge_id = str(edge.get("id", "unknown"))
        if mismatch > tolerance_feet:
            failures.append(
                f"Shared edge {edge_id} has incompatible pitch/direction constraints "
                f"({mismatch:.3f} ft endpoint difference; tolerance {tolerance_feet:.3f} ft)."
            )
        constraints.append((first_id, second_id, sum(deltas) / 2, edge_id))

    adjacency: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for first, second, delta, edge_id in constraints:
        adjacency[first].append((second, delta, edge_id))
        adjacency[second].append((first, -delta, edge_id))

    offsets: dict[str, float] = {}
    components: list[set[str]] = []
    for root in planes:
        if root in offsets:
            continue
        offsets[root] = 0.0
        component = {root}
        queue: deque[str] = deque([root])
        while queue:
            current = queue.popleft()
            for neighbor, delta, edge_id in adjacency.get(current, []):
                proposed = offsets[current] + delta
                if neighbor in offsets:
                    mismatch = abs(offsets[neighbor] - proposed)
                    if mismatch > tolerance_feet:
                        failures.append(
                            f"Shared-edge cycle conflicts at {edge_id} by {mismatch:.3f} ft "
                            f"(tolerance {tolerance_feet:.3f} ft)."
                        )
                    continue
                offsets[neighbor] = proposed
                component.add(neighbor)
                queue.append(neighbor)
        components.append(component)

    eave_planes = {
        plane_id
        for edge in topology.get("edges", [])
        if edge.get("id") in eave_edge_ids
        for plane_id in edge.get("adjacentPlaneIds", [])
        if plane_id in planes
    }
    anchored: set[str] = set()
    component_by_plane: dict[str, int] = {}
    component_shifts = [0.0 for _ in components]
    for component_index, component in enumerate(components):
        component_by_plane.update({plane_id: component_index for plane_id in component})
        eave_members = component & eave_planes
        if not eave_members:
            warnings.append(
                "A disconnected roof component has no classified eave edge; "
                "its vertical datum is unknown."
            )
            continue
        eave_vertex_ids = {
            vertex_id
            for edge in topology.get("edges", [])
            if edge.get("id") in eave_edge_ids
            and set(edge.get("adjacentPlaneIds", [])) & eave_members
            for vertex_id in edge.get("vertexIds", [])
        }
        heights_at_eave: list[float] = []
        for plane_id in eave_members:
            gradient = gradients[plane_id]
            for vertex_id in planes[plane_id].get("boundaryVertexIds", []):
                if vertex_id not in eave_vertex_ids:
                    continue
                point = vertices[vertex_id]
                heights_at_eave.append(
                    offsets[plane_id]
                    + gradient[0] * float(point[0])
                    + gradient[1] * float(point[1])
                )
        if not heights_at_eave:
            failures.append("A classified eave has no vertices attached to a plane.")
            continue
        shift = -min(heights_at_eave)
        for plane_id in component:
            offsets[plane_id] += shift
            anchored.add(plane_id)

    # Roofr diagrams sometimes show one roof section overlapping another without
    # a shared edge. The report gives their order, but not the vertical gap. Keep
    # every pitch/shared-edge solution intact and move an entire disconnected
    # component only as far as needed to honor the stated order. Such offsets are
    # explicitly reported as estimates.
    ordering_constraints: list[
        tuple[int, int, float, str, str, tuple[float, float]]
    ] = []
    for relation in overlap_order:
        above_id = str(relation.get("abovePlaneId", ""))
        below_id = str(relation.get("belowPlaneId", ""))
        if above_id not in planes or below_id not in planes:
            failures.append(
                f"Overlap ordering references an unknown plane ({above_id}, {below_id})."
            )
            continue
        witness = relation.get("witnessPlan")
        if (
            not isinstance(witness, Sequence)
            or isinstance(witness, (str, bytes))
            or len(witness) != 2
        ):
            failures.append(f"Overlap relation {above_id}>{below_id} has no valid witness point.")
            continue
        try:
            witness_point = (float(witness[0]), float(witness[1]))
        except (TypeError, ValueError):
            failures.append(f"Overlap relation {above_id}>{below_id} has no valid witness point.")
            continue
        if not all(math.isfinite(coordinate) for coordinate in witness_point):
            failures.append(f"Overlap relation {above_id}>{below_id} has no valid witness point.")
            continue
        above_component = component_by_plane.get(above_id)
        below_component = component_by_plane.get(below_id)
        if above_component is None or below_component is None:
            failures.append(
                f"Overlap relation {above_id}>{below_id} includes a plane without a solved datum."
            )
            continue
        above_height = _height_at(
            planes[above_id], gradients[above_id], offsets[above_id], witness_point
        )
        below_height = _height_at(
            planes[below_id], gradients[below_id], offsets[below_id], witness_point
        )
        if above_height is None or below_height is None:
            failures.append(f"Overlap witness is outside {above_id} or {below_id}.")
            continue
        minimum_offset_difference = below_height - above_height + overlap_gap_feet
        if above_component == below_component:
            if minimum_offset_difference > 1e-8:
                failures.append(
                    f"Page stacking requires {above_id} above {below_id}, but shared-edge "
                    "constraints place both planes in one rigid height component."
                )
            continue
        ordering_constraints.append(
            (
                above_component,
                below_component,
                minimum_offset_difference,
                above_id,
                below_id,
                witness_point,
            )
        )

    # These are lower-bound difference constraints. Starting every disconnected
    # component at its lowest eave datum and relaxing upward finds the smallest
    # non-negative shifts for an acyclic stacking graph. A positive cycle means
    # the source constraints cannot all be true at once.
    for _iteration in range(len(components)):
        changed = False
        for (
            above_component,
            below_component,
            required,
            *_,
        ) in ordering_constraints:
            proposed = component_shifts[below_component] + required
            if component_shifts[above_component] + 1e-8 < proposed:
                component_shifts[above_component] = proposed
                changed = True
        if not changed:
            break
    else:
        failures.append("Page stacking relationships contain a contradictory vertical cycle.")

    for component_index, shift in enumerate(component_shifts):
        if shift <= 1e-8:
            continue
        for plane_id in components[component_index]:
            offsets[plane_id] += shift
        warnings.append(
            f"An estimated {shift:.3f} ft vertical offset was applied to honor report stacking "
            f"order for roof component {', '.join(sorted(components[component_index]))}. "
            "The report specifies which section is above, not the exact separation."
        )

    solved_heights: dict[str, float] = {}
    for plane_id, plane in planes.items():
        if plane_id not in anchored:
            continue
        gradient = gradients[plane_id]
        for vertex_id in plane.get("boundaryVertexIds", []):
            point = vertices[vertex_id]
            height = (
                offsets[plane_id] + gradient[0] * float(point[0]) + gradient[1] * float(point[1])
            )
            previous = solved_heights.get(vertex_id)
            if previous is not None:
                mismatch = abs(previous - height)
                if mismatch > tolerance_feet:
                    failures.append(
                        f"Shared vertex {vertex_id} has contradictory heights "
                        f"({previous:.3f} ft and {height:.3f} ft)."
                    )
                elif mismatch > 1e-8:
                    warnings.append(
                        f"Shared vertex {vertex_id} differs by {mismatch:.3f} ft within solver "
                        "tolerance; the first solved plane's height was retained."
                    )
            else:
                solved_heights[vertex_id] = height

    unresolved = set(planes) - anchored
    if unresolved:
        failures.append(
            "Height solver lacks a lowest-eave datum for planes: " + ", ".join(sorted(unresolved))
        )

    for _, _, _, above_id, below_id, witness in ordering_constraints:
        above_height = _height_at(planes[above_id], gradients[above_id], offsets[above_id], witness)
        below_height = _height_at(planes[below_id], gradients[below_id], offsets[below_id], witness)
        if (
            above_height is None
            or below_height is None
            or above_height < below_height + overlap_gap_feet - 1e-8
        ):
            failures.append(f"Page stacking order {above_id}>{below_id} could not be satisfied.")

    status: Literal["solved", "needs_review"] = "needs_review" if failures else "solved"
    return _result(status, solved_heights, warnings, failures, constraints)


def _height_at(
    plane: dict[str, Any],
    gradient: tuple[float, float],
    offset: float,
    point: Sequence[float],
) -> float | None:
    # Overlap witnesses may lie in overlapping surfaces and not on a shared vertex.
    vertices = plane.get("planPolygon")
    if vertices and not _point_in_polygon((float(point[0]), float(point[1])), vertices):
        return None
    return offset + gradient[0] * float(point[0]) + gradient[1] * float(point[1])


def _point_in_polygon(point: tuple[float, float], polygon: Sequence[Sequence[float]]) -> bool:
    inside = False
    x, y = point
    for index, vertex in enumerate(polygon):
        next_vertex = polygon[(index + 1) % len(polygon)]
        x1, y1 = float(vertex[0]), float(vertex[1])
        x2, y2 = float(next_vertex[0]), float(next_vertex[1])
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _result(
    status: Literal["solved", "needs_review"],
    heights: dict[str, float],
    warnings: list[str],
    failures: list[str],
    constraints: list[tuple[str, str, float, str]],
) -> dict[str, Any]:
    return {
        "status": status if not failures else "needs_review",
        "vertexHeights": {key: round(value, 6) for key, value in heights.items()},
        "warnings": list(dict.fromkeys(warnings)),
        "failures": list(dict.fromkeys(failures)),
        "sharedEdgeConstraints": [
            {"planeA": first, "planeB": second, "heightOffsetFt": delta, "edgeId": edge_id}
            for first, second, delta, edge_id in constraints
        ],
        "estimated": True,
        "estimatedHeightNote": (
            "Eave datum is relative; absolute elevation above ground is not in the report."
        ),
    }
