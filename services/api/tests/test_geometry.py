from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import fitz
import pytest

from app.geometry.calibrate import (
    CalibrationSegment,
    DimensionObservation,
    fit_both_scale_hypotheses,
    fit_scale,
)
from app.geometry.diagram_extract import analyze_pdf_geometry, read_vector_page
from app.geometry.heights import solve_plane_heights
from app.geometry.migrations import migrate_geometry
from app.geometry.roofr import _arrow_shapes
from app.geometry.topology import (
    build_topology,
    point_in_polygon,
    polygon_area,
    polygonize_segments,
)
from app.geometry.validate import (
    validate_class_totals,
    validate_pitch_gradients,
    validate_review_geometry,
)
from app.geometry.workflow import approval_blockers, build_review_geometry

ROOT = Path(__file__).resolve().parents[3]
ROOFR_PDF = ROOT / "83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf"
MANAGER_PDF = ROOT / "RoofManager-Example-Report.pdf"
DEACON_PDF = ROOT / "17 Deacon Place, St_ Albert, AB T8N 6S5.pdf"


def _pitch(rise: float) -> dict[str, float | str]:
    return {"notation": f"{rise:g}:12", "rise": rise, "run": 12.0}


def _facet(
    facet_id: str,
    points: list[list[float]],
    *,
    rise: float,
    direction: tuple[float, float],
    edges: list[str],
    roof_type: str = "pitched",
    draw_order: int = 0,
) -> dict[str, Any]:
    return {
        "id": facet_id,
        "name": facet_id,
        "roofType": roof_type,
        "polygonPagePoints": points,
        "pitch": _pitch(rise),
        "downslopeDirectionPage": direction,
        "edgeTypes": edges,
        "drawOrder": draw_order,
        "areaLabelSqFt": None,
    }


def _write_synthetic_pdf(path: Path, facets: list[dict[str, Any]]) -> None:
    document = fitz.open()
    page = document.new_page(width=240, height=320)
    page_offset = (50.0, 120.0)
    edge_colors = {
        "eave": (0.1, 0.6, 0.25),
        "rake": (0.9, 0.7, 0.1),
        "ridge": (0.8, 0.1, 0.1),
        "hip": (0.5, 0.2, 0.75),
        "valley": (0.1, 0.2, 0.8),
        "transition": (0.1, 0.65, 0.7),
    }
    edge_classes: dict[tuple[tuple[float, float], tuple[float, float]], str] = {}
    for facet in facets:
        points = [tuple(point) for point in facet["polygonPagePoints"]]
        for index, point in enumerate(points):
            following = points[(index + 1) % len(points)]
            key = tuple(sorted((point, following)))
            edge_classes[key] = str(facet["edgeTypes"][index])

    for key, edge_class in edge_classes.items():
        start, end = key
        page.draw_line(
            (start[0] + page_offset[0], start[1] + page_offset[1]),
            (end[0] + page_offset[0], end[1] + page_offset[1]),
            color=edge_colors[edge_class],
            width=1,
        )
    for index, edge_class in enumerate(sorted(set(edge_classes.values()))):
        y = 18 + index * 12
        page.draw_line((15, y), (28, y), color=edge_colors[edge_class], width=1.5)
        page.insert_text((32, y + 2), edge_class.replace("_", " ").title(), fontsize=6)

    for facet in facets:
        points = [tuple(point) for point in facet["polygonPagePoints"]]
        centroid = (
            sum(point[0] for point in points) / len(points) + page_offset[0],
            sum(point[1] for point in points) / len(points) + page_offset[1],
        )
        page.insert_text(centroid, str(int(facet["pitch"]["rise"])), fontsize=7)
        page.insert_text((centroid[0], centroid[1] + 8), facet["id"], fontsize=6)
        # A compact vector arrow: a shaft and two head strokes. Its direction is
        # the same evidence the Roofr adapter reads from the source pitch page.
        dx, dy = facet["downslopeDirectionPage"]
        magnitude = (dx**2 + dy**2) ** 0.5
        if magnitude:
            ux, uy = dx / magnitude, dy / magnitude
            tip = (centroid[0] + ux * 4, centroid[1] + uy * 4)
            tail = (centroid[0] - ux * 4, centroid[1] - uy * 4)
            shape = page.new_shape()
            shape.draw_line(tail, tip)
            shape.draw_line(tip, (tip[0] - ux * 1.5 - uy, tip[1] - uy * 1.5 + ux))
            shape.draw_line(tip, (tip[0] - ux * 1.5 + uy, tip[1] - uy * 1.5 - ux))
            shape.finish(color=(0.16, 0.22, 0.24), fill=(0.16, 0.22, 0.24), width=1)
            shape.commit()
    document.save(path)
    document.close()


@pytest.mark.parametrize(
    ("facets", "expected_regions", "edge_type"),
    [
        (
            [
                _facet(
                    "south",
                    [[0, 100], [100, 100], [50, 50]],
                    rise=4,
                    direction=(0, 1),
                    edges=["eave", "hip", "hip"],
                ),
                _facet(
                    "east",
                    [[100, 100], [100, 0], [50, 50]],
                    rise=4,
                    direction=(-1, 0),
                    edges=["eave", "hip", "hip"],
                ),
                _facet(
                    "north",
                    [[100, 0], [0, 0], [50, 50]],
                    rise=4,
                    direction=(0, -1),
                    edges=["eave", "hip", "hip"],
                ),
                _facet(
                    "west",
                    [[0, 0], [0, 100], [50, 50]],
                    rise=4,
                    direction=(1, 0),
                    edges=["eave", "hip", "hip"],
                ),
            ],
            4,
            "hip",
        ),
        (
            [
                _facet(
                    "upper-leg",
                    [[0, 0], [100, 0], [100, 40], [40, 40], [0, 40]],
                    rise=3,
                    direction=(0, 1),
                    edges=["eave", "rake", "rake", "valley", "rake"],
                ),
                _facet(
                    "left-leg",
                    [[0, 40], [40, 40], [40, 100], [0, 100]],
                    rise=3,
                    direction=(0, 1),
                    edges=["valley", "transition", "rake", "eave"],
                ),
            ],
            2,
            "valley",
        ),
        (
            [
                _facet(
                    "upper-slope",
                    [[0, 0], [100, 0], [100, 50], [0, 50]],
                    rise=6,
                    direction=(0, 1),
                    edges=["rake", "rake", "transition", "rake"],
                ),
                _facet(
                    "lower-slope",
                    [[0, 50], [100, 50], [100, 100], [0, 100]],
                    rise=3,
                    direction=(0, 1),
                    edges=["transition", "rake", "eave", "rake"],
                ),
            ],
            2,
            "transition",
        ),
        (
            [
                _facet(
                    "flat-drain",
                    [[0, 0], [100, 0], [100, 60], [0, 60]],
                    rise=1,
                    direction=(0, 1),
                    edges=["rake", "rake", "eave", "rake"],
                    roof_type="flat",
                ),
            ],
            1,
            "eave",
        ),
    ],
    ids=["synthetic-hip", "synthetic-l-valley", "synthetic-transition", "synthetic-flat"],
)
def test_synthetic_vector_pdf_outline_pitch_and_height_pipeline(
    facets: list[dict[str, Any]], expected_regions: int, edge_type: str
) -> None:
    with TemporaryDirectory(prefix="roof-geometry-test-") as temp:
        pdf_path = Path(temp) / "synthetic.pdf"
        page_offset = (50.0, 120.0)
        _write_synthetic_pdf(pdf_path, facets)
        document = fitz.open(pdf_path)
        page = read_vector_page(document[0], 1)
        document.close()
        polygons, gaps = polygonize_segments(
            (
                segment.segment
                for segment in page.segments
                if segment.edge_class and segment.length_points > 20
            ),
            snap_tolerance=0.1,
            minimum_area=10,
        )
        assert not gaps
        assert len(polygons) == expected_regions
        assert all(polygon_area(polygon) > 0 for polygon in polygons)
        assert set(page.color_legend.values()) >= {
            edge_class for facet in facets for edge_class in facet["edgeTypes"]
        }
        assert sorted(round(polygon_area(polygon), 2) for polygon in polygons) == sorted(
            round(polygon_area([tuple(point) for point in facet["polygonPagePoints"]]), 2)
            for facet in facets
        )

        arrows = _arrow_shapes(page)
        numeric_pitch_words = [word for word in page.words if word.text.isdigit()]
        assert len(arrows) == len(facets)
        assert len(numeric_pitch_words) == len(facets)
        recovered_pitch_and_direction: dict[str, tuple[dict[str, Any], tuple[float, float]]] = {}
        for facet in facets:
            polygon = [
                (point[0] + page_offset[0], point[1] + page_offset[1])
                for point in facet["polygonPagePoints"]
            ]
            label_matches = [
                word for word in numeric_pitch_words if point_in_polygon(word.center, polygon)
            ]
            assert len(label_matches) == 1
            label = label_matches[0]
            arrow = min(arrows, key=lambda item: math.dist(item["center"], label.center))
            assert math.dist(arrow["center"], label.center) < 20
            expected_direction = facet["downslopeDirectionPage"]
            dot = (
                arrow["direction"][0] * expected_direction[0]
                + arrow["direction"][1] * expected_direction[1]
            )
            assert dot > 0.95
            recovered_pitch_and_direction[facet["id"]] = (
                _pitch(float(label.text)),
                (float(arrow["direction"][0]), float(arrow["direction"][1])),
            )

    edge_types_by_key: dict[tuple[tuple[float, float], tuple[float, float]], str] = {}
    vector_page = page
    for segment in vector_page.segments:
        if segment.edge_class:
            start = (segment.start[0] - page_offset[0], segment.start[1] - page_offset[1])
            end = (segment.end[0] - page_offset[0], segment.end[1] - page_offset[1])
            edge_types_by_key[tuple(sorted((start, end)))] = segment.edge_class

    expected_planes = [
        {
            "id": facet["id"],
            "name": facet["name"],
            "roofType": facet["roofType"],
            "vertices": facet["polygonPagePoints"],
            "reportedAreaSqFt": None,
            "pitch": recovered_pitch_and_direction[facet["id"]][0],
            "downslopePlanUnitVector": [
                recovered_pitch_and_direction[facet["id"]][1][0],
                -recovered_pitch_and_direction[facet["id"]][1][1],
            ],
            "drawOrder": facet["drawOrder"],
            "edgeTypes": facet["edgeTypes"],
            "pagePoints": facet["polygonPagePoints"],
        }
        for facet in facets
    ]
    topology = build_topology(
        expected_planes,
        snap_tolerance=0.1,
        edge_classifier=lambda start, end: edge_types_by_key.get(
            tuple(sorted((start, end))), edge_type
        ),
    )
    eave_ids = {edge["id"] for edge in topology["edges"] if edge["type"] == "eave"}
    solved = solve_plane_heights(
        {key: topology[key] for key in ("vertices", "planes", "edges")},
        eave_edge_ids=eave_ids,
    )
    if edge_type == "transition":
        assert solved["status"] == "solved", solved["failures"]
    elif edge_type == "hip":
        assert solved["status"] == "solved", solved["failures"]
    elif edge_type == "eave":
        assert solved["status"] == "solved", solved["failures"]
    else:
        assert topology["planes"] and any(edge["type"] == "valley" for edge in topology["edges"])
    shared_vertices: dict[str, set[float]] = {}
    for plane in topology["planes"]:
        for vertex_id in plane["boundaryVertexIds"]:
            height = solved["vertexHeights"].get(vertex_id)
            if height is not None:
                shared_vertices.setdefault(vertex_id, set()).add(height)
    assert all(len(heights) == 1 for heights in shared_vertices.values())


def test_scale_calibration_selects_sloped_hypothesis_and_rejects_outlier() -> None:
    segments = [
        CalibrationSegment(
            f"edge-{index}",
            (0.0, float(index * 20)),
            (10.0, float(index * 20)),
            3,
            edge_class="rake",
            sloped_factor=1.2,
        )
        for index in range(1, 6)
    ]
    observations = [
        DimensionObservation(
            f"label-{index}",
            value,
            (5.0, float(index * 20)),
            3,
            edge_class="rake",
            rounding_step_feet=0.01,
            rounded_up=False,
        )
        for index, value in enumerate((5.0, 10.0, 18.0, 24.0, 80.0), start=1)
    ]
    segments = [
        CalibrationSegment(
            f"edge-{index}",
            (0.0, float(index * 20)),
            (length, float(index * 20)),
            3,
            edge_class="rake",
            sloped_factor=factor,
        )
        for index, (length, factor) in enumerate(
            ((10, 1.0), (20, 1.0), (30, 1.2), (40, 1.2), (50, 1.2)),
            start=1,
        )
    ]
    plan, sloped, chosen = fit_both_scale_hypotheses(
        observations, segments, max_label_distance_points=2, minimum_inliers=3
    )
    assert chosen == "sloped"
    assert sloped.status == "calibrated"
    assert sloped.inlier_count == 4
    assert plan.inlier_count < sloped.inlier_count
    assert sloped.feet_per_point == pytest.approx(0.5, abs=0.01)


def test_scale_calibration_accepts_display_rounded_up_dimensions() -> None:
    segments = [
        CalibrationSegment(
            f"edge-{index}",
            (0.0, float(index * 20)),
            (length, float(index * 20)),
            3,
            edge_class="eave",
        )
        for index, length in enumerate((11.0, 21.0, 41.0, 61.0), start=1)
    ]
    observations = [
        DimensionObservation(
            f"label-{index}",
            value,
            (length / 2, float(index * 20)),
            3,
            edge_class="eave",
            rounding_step_feet=1.0,
            rounded_up=True,
        )
        for index, (length, value) in enumerate(
            ((11.0, 6.0), (21.0, 11.0), (41.0, 21.0), (61.0, 31.0)),
            start=1,
        )
    ]
    fit = fit_scale(observations, segments, minimum_inliers=3)
    assert fit.status == "calibrated"
    assert fit.inlier_count == 4
    assert fit.feet_per_point == pytest.approx(0.5, abs=0.01)


def test_height_solver_rejects_contradictory_and_insufficient_constraints() -> None:
    planes = [
        {
            "id": "north",
            "boundaryVertexIds": ["a", "b", "c", "d"],
            "pitch": {"rise": 4, "run": 12, "downslopePlanUnitVector": [1, 0]},
            "planPolygon": [[0, 0], [10, 0], [10, 5], [0, 5]],
        },
        {
            "id": "south",
            "boundaryVertexIds": ["d", "c", "e", "f"],
            "pitch": {"rise": 6, "run": 12, "downslopePlanUnitVector": [0, 1]},
            "planPolygon": [[0, 5], [10, 5], [10, 10], [0, 10]],
        },
    ]
    topology = {
        "vertices": [
            {"id": "a", "plan": [0, 0, 0]},
            {"id": "b", "plan": [10, 0, 0]},
            {"id": "c", "plan": [10, 5, 0]},
            {"id": "d", "plan": [0, 5, 0]},
            {"id": "e", "plan": [10, 10, 0]},
            {"id": "f", "plan": [0, 10, 0]},
        ],
        "planes": planes,
        "edges": [
            {"id": "eave-north", "vertexIds": ["a", "b"], "adjacentPlaneIds": ["north"]},
            {"id": "transition", "vertexIds": ["c", "d"], "adjacentPlaneIds": ["north", "south"]},
        ],
    }
    contradicted = solve_plane_heights(topology, eave_edge_ids={"eave-north"}, tolerance_feet=0.01)
    assert contradicted["status"] == "needs_review"
    assert any("incompatible pitch/direction" in item for item in contradicted["failures"])
    insufficient = solve_plane_heights(topology, eave_edge_ids=set())
    assert insufficient["status"] == "needs_review"
    assert any("datum" in item for item in insufficient["failures"])


def test_height_solver_applies_minimum_estimated_offset_for_report_stacking_order() -> None:
    topology = {
        "vertices": [
            {"id": "a0", "plan": [0.0, 0.0, 0.0]},
            {"id": "a1", "plan": [10.0, 0.0, 0.0]},
            {"id": "a2", "plan": [10.0, 10.0, 0.0]},
            {"id": "a3", "plan": [0.0, 10.0, 0.0]},
            {"id": "b0", "plan": [5.0, 0.0, 0.0]},
            {"id": "b1", "plan": [15.0, 0.0, 0.0]},
            {"id": "b2", "plan": [15.0, 10.0, 0.0]},
            {"id": "b3", "plan": [5.0, 10.0, 0.0]},
        ],
        "planes": [
            {
                "id": "upper",
                "boundaryVertexIds": ["a0", "a1", "a2", "a3"],
                "planPolygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                "pitch": {"rise": 4, "run": 12, "downslopePlanUnitVector": [1, 0]},
            },
            {
                "id": "lower",
                "boundaryVertexIds": ["b0", "b1", "b2", "b3"],
                "planPolygon": [[5.0, 0.0], [15.0, 0.0], [15.0, 10.0], [5.0, 10.0]],
                "pitch": {"rise": 4, "run": 12, "downslopePlanUnitVector": [1, 0]},
            },
        ],
        "edges": [
            {"id": "upper-eave", "vertexIds": ["a1", "a2"], "adjacentPlaneIds": ["upper"]},
            {"id": "lower-eave", "vertexIds": ["b1", "b2"], "adjacentPlaneIds": ["lower"]},
        ],
    }
    solved = solve_plane_heights(
        topology,
        eave_edge_ids={"upper-eave", "lower-eave"},
        overlap_order=[
            {
                "abovePlaneId": "upper",
                "belowPlaneId": "lower",
                "witnessPlan": [7.0, 5.0],
            }
        ],
    )
    assert solved["status"] == "solved", solved["failures"]
    assert any(
        "estimated" in warning and "vertical offset" in warning
        for warning in solved["warnings"]
    )

    offsets: dict[str, float] = {}
    for plane_id, vertex_id, x in (("upper", "a0", 0.0), ("lower", "b0", 5.0)):
        # Both planes have the same x-directed 4:12 pitch; infer each plane's
        # offset from one solved boundary vertex.
        offsets[plane_id] = solved["vertexHeights"][vertex_id] + x / 3
    upper_height = offsets["upper"] - 7.0 / 3
    lower_height = offsets["lower"] - 7.0 / 3
    assert upper_height >= lower_height + 0.01 - 1e-6


def test_height_solver_reports_small_offsets_needed_to_preserve_strict_stacking() -> None:
    topology = {
        "vertices": [
            {"id": f"{prefix}{index}", "plan": point}
            for prefix in ("a", "b")
            for index, point in enumerate(
                ([0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0])
            )
        ],
        "planes": [
            {
                "id": plane_id,
                "boundaryVertexIds": [f"{prefix}{index}" for index in range(4)],
                "planPolygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                "pitch": {"rise": 4, "run": 12, "downslopePlanUnitVector": [1, 0]},
            }
            for plane_id, prefix in (("upper", "a"), ("lower", "b"))
        ],
        "edges": [
            {
                "id": f"{prefix}-eave",
                "vertexIds": [f"{prefix}1", f"{prefix}2"],
                "adjacentPlaneIds": [plane_id],
            }
            for plane_id, prefix in (("upper", "a"), ("lower", "b"))
        ],
    }
    solved = solve_plane_heights(
        topology,
        eave_edge_ids={"a-eave", "b-eave"},
        overlap_order=[
            {
                "abovePlaneId": "upper",
                "belowPlaneId": "lower",
                "witnessPlan": [5.0, 5.0],
            }
        ],
    )
    assert solved["status"] == "solved", solved["failures"]
    assert any("0.010 ft vertical offset" in warning for warning in solved["warnings"])


def test_height_solver_keeps_the_first_height_for_sub_tolerance_shared_vertices() -> None:
    topology = {
        "vertices": [
            {"id": "a0", "plan": [0.0, 0.0, 0.0]},
            {"id": "a1", "plan": [0.0, 10.0, 0.0]},
            {"id": "b0", "plan": [0.0, 0.0, 0.0]},
            {"id": "b1", "plan": [0.0, 10.0, 0.0]},
            {"id": "shared", "plan": [1.0, 5.0, 0.0]},
            {"id": "a2", "plan": [1.0, 0.0, 0.0]},
            {"id": "b2", "plan": [1.0, 10.0, 0.0]},
        ],
        "planes": [
            {
                "id": "plane-a",
                "boundaryVertexIds": ["a0", "a2", "shared", "a1"],
                "planPolygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 5.0], [0.0, 10.0]],
                "pitch": {"rise": 4, "run": 12, "downslopePlanUnitVector": [1, 0]},
            },
            {
                "id": "plane-b",
                "boundaryVertexIds": ["b0", "shared", "b2", "b1"],
                "planPolygon": [[0.0, 0.0], [1.0, 5.0], [1.0, 10.0], [0.0, 10.0]],
                "pitch": {"rise": 4.1, "run": 12, "downslopePlanUnitVector": [1, 0]},
            },
        ],
        "edges": [
            {"id": "eave-a", "vertexIds": ["a0", "a1"], "adjacentPlaneIds": ["plane-a"]},
            {"id": "eave-b", "vertexIds": ["b0", "b1"], "adjacentPlaneIds": ["plane-b"]},
        ],
    }
    solved = solve_plane_heights(topology, eave_edge_ids={"eave-a", "eave-b"})
    assert solved["status"] == "solved", solved["failures"]
    assert solved["vertexHeights"]["shared"] == pytest.approx(-1 / 3)
    assert any(
        "first solved plane's height was retained" in warning for warning in solved["warnings"]
    )


def test_review_geometry_uses_report_scale_and_leaves_missing_direction_unbuilt() -> None:
    geometry, diagnostics = build_review_geometry(
        [
            _facet(
                "plane-a",
                [[0, 0], [20, 0], [20, 10], [0, 10]],
                rise=4,
                direction=(0, -1),
                edges=["rake", "rake", "eave", "rake"],
            ),
            {
                **_facet(
                    "plane-b",
                    [[20, 0], [40, 0], [40, 10], [20, 10]],
                    rise=4,
                    direction=(0, -1),
                    edges=["rake", "rake", "eave", "rake"],
                ),
                "downslopeDirectionPage": None,
            },
        ],
        feet_per_point=0.5,
        origin_page_point=(0, 10),
        height_tolerance_feet=0.05,
        source="manual",
        confidence="medium",
        report_name="synthetic.pdf",
    )
    assert geometry is not None
    assert geometry["schemaVersion"] == "1.2"
    assert len(geometry["planes"]) == 1
    assert any("plane-b" in warning for warning in diagnostics["warnings"])
    assert geometry["planes"][0]["reportedAreaSqFt"] is None


def test_geometry_v1_migration_adds_provenance_and_preserves_known_values() -> None:
    legacy = {
        "schemaVersion": "1.0",
        "planes": [{"id": "known", "reportedAreaSqFt": 50}, {"id": "unknown"}],
    }
    migrated = migrate_geometry(legacy)
    assert migrated["schemaVersion"] == "1.2"
    assert migrated["planes"] == [
        {
            "id": "known",
            "reportedAreaSqFt": 50.0,
            "provenance": {
                "outline": "reported",
                "pitch": "reported",
                "downslopeDirection": "reported",
                "area": "reported",
            },
            "confidence": 0.5,
        },
        {
            "id": "unknown",
            "reportedAreaSqFt": None,
            "provenance": {
                "outline": "reported",
                "pitch": "reported",
                "downslopeDirection": "reported",
                "area": "reported",
            },
            "confidence": 0.5,
        },
    ]
    with pytest.raises(ValueError, match="Unsupported"):
        migrate_geometry({"schemaVersion": "9.0", "planes": []})


def test_approval_is_blocked_for_warning_checks_or_missing_geometry() -> None:
    analysis = {
        "facets": [{"id": "plane-a", "pitch": _pitch(4), "downslopeDirectionPage": [0, 1]}],
        "validation": {"checks": [{"name": "edge-class", "status": "warn"}]},
    }
    assert any("warn" in item for item in approval_blockers(analysis, None))


def test_missing_edge_class_is_zero_only_when_reported_zero() -> None:
    checks = validate_class_totals(
        {"eave": 10.0},
        {"eave": 10.0, "valley": 0.0, "wall_flashing": 4.0},
        tolerance_percent=3,
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["edge_class:valley"]["status"] == "pass"
    assert by_name["edge_class:valley"]["actual"] == 0.0
    assert by_name["edge_class:wall_flashing"]["status"] == "not_evaluable"


def test_review_recomputes_geometry_dependent_checks_instead_of_reusing_source_results() -> None:
    analysis = {
        "expectedFacetCount": 1,
        "reported": {"edgeTotalsFt": {"eave": 2.0}},
        "validation": {
            "checks": [
                {"name": "dimension_scale", "status": "pass", "actual": 1.0},
                {"name": "facet:old:slope_area", "status": "fail", "actual": 99.0},
                {"name": "facet_count", "status": "warn", "actual": 0},
                {"name": "edge_class:eave", "status": "warn", "actual": 99.0},
                {"name": "pitch_gradient:old", "status": "not_evaluable"},
                {"name": "geometry_sloped_area", "status": "fail", "actual": 99.0},
            ]
        },
    }
    facets = [
        {
            "id": "edited",
            "areaLabelSqFt": 2.0,
            "feetPerPoint": 1.0,
            "polygonPagePoints": [[0, 0], [2, 0], [0, 2]],
            "pitch": {"rise": 0, "run": 12},
        }
    ]
    geometry = {
        "heightSolve": {"status": "solved"},
        "vertices": [
            {"id": "a", "plan": [0.0, 0.0, 0.0]},
            {"id": "b", "plan": [2.0, 0.0, 0.0]},
            {"id": "c", "plan": [0.0, 2.0, 0.0]},
        ],
        "planes": [{"id": "edited", "boundaryVertexIds": [], "pitch": None}],
        "edges": [{"type": "eave", "vertexIds": ["a", "b"]}],
    }

    checks = validate_review_geometry(
        analysis,
        facets,
        geometry,
        feet_per_point=1.0,
        scale_tolerance_percent=3.0,
        area_tolerance_percent=5.0,
        edge_tolerance_percent=3.0,
    )
    by_name = {check["name"]: check for check in checks}

    assert by_name["facet:edited:slope_area"]["status"] == "pass"
    assert by_name["facet_count"]["status"] == "pass"
    assert by_name["edge_class:eave"]["status"] == "pass"
    assert by_name["edge_class:eave"]["actual"] == 2.0
    assert "facet:old:slope_area" not in by_name
    assert "pitch_gradient:old" not in by_name
    assert "geometry_sloped_area" not in by_name


def test_solved_mesh_pitch_is_checked_against_the_label_tolerance() -> None:
    geometry = {
        "heightSolve": {"status": "solved"},
        "vertices": [
            {"id": "a", "plan": [0.0, 0.0, 4.0]},
            {"id": "b", "plan": [12.0, 0.0, 0.0]},
            {"id": "c", "plan": [12.0, 12.0, 0.0]},
            {"id": "d", "plan": [0.0, 12.0, 4.0]},
        ],
        "planes": [
            {
                "id": "plane-a",
                "boundaryVertexIds": ["a", "b", "c", "d"],
                "pitch": {"rise": 4.0, "run": 12.0},
            }
        ],
    }
    check = validate_pitch_gradients(geometry, tolerance_degrees=0.25)[0]
    assert check["status"] == "pass"
    assert check["actual"] == pytest.approx(18.434949, abs=1e-6)

    geometry["vertices"][1]["plan"][2] = 1.0
    geometry["vertices"][2]["plan"][2] = 1.0
    check = validate_pitch_gradients(geometry, tolerance_degrees=0.25)[0]
    assert check["status"] == "warn"
    assert check["differenceDegrees"] > 4


def test_roofr_golden_vector_recovery_keeps_all_eight_facets_and_reports_diagnostics() -> None:
    if not ROOFR_PDF.exists():
        pytest.skip("Roofr golden PDF fixture is not available in this checkout")
    analysis = analyze_pdf_geometry(ROOFR_PDF)
    assert analysis["sourceFormat"] == "roofr"
    assert analysis["facetCount"] == 8
    assert sorted(facet["areaLabelSqFt"] for facet in analysis["facets"]) == [
        12.0,
        18.0,
        56.0,
        93.0,
        176.0,
        190.0,
        212.0,
        406.0,
    ]
    assert sum(bool(facet.get("downslopeDirectionPage")) for facet in analysis["facets"]) == 6
    assert sum(bool(facet.get("pitch")) for facet in analysis["facets"]) == 8
    assert analysis["diagnostics"]["unmatchedAreaLabels"] == []
    assert len(analysis["diagnostics"]["unmatchedPitchLabels"]) == 2
    assert analysis["validation"]["scaleHypotheses"]["chosen"] == "sloped"
    assert analysis["validation"]["scaleHypotheses"]["sloped"]["inlierCount"] >= 8
    assert analysis["validation"]["scaleHypotheses"]["sloped"]["observationCount"] == 12
    facet_checks = [
        check for check in analysis["validation"]["checks"] if check["name"].startswith("facet:")
    ]
    assert all(check["status"] in {"pass", "warn"} for check in facet_checks)
    assert any(check["status"] == "warn" for check in facet_checks)
    assert analysis["status"] == "needs_review"
    assert analysis["geometry"]["status"] == "needs_review"
    assert analysis["geometry"]["estimatedHeights"] is True
    assert any("Shared edge" in warning for warning in analysis["warnings"])


def test_deacon_roofr_golden_does_not_average_conflicting_plane_heights() -> None:
    if not DEACON_PDF.exists():
        pytest.skip("17 Deacon Place golden PDF fixture is not available in this checkout")
    analysis = analyze_pdf_geometry(DEACON_PDF)
    geometry = analysis["geometry"]
    assert geometry is not None
    assert analysis["facetCount"] == 16
    pitch_labels = [
        facet["pitch"]["notation"] for facet in analysis["facets"] if facet.get("pitch")
    ]
    assert sorted(pitch_labels) == ["4:12", *(["5:12"] * 13)]
    assert geometry["heightSolve"]["status"] == "needs_review"
    assert any(
        "incompatible pitch/direction" in failure
        for failure in geometry["heightSolve"]["failures"]
    )
    assert len(geometry["heightSolve"]["vertexHeights"]) < len(geometry["vertices"])


def test_roof_manager_golden_keeps_31_reported_planes_unreconstructed() -> None:
    if not MANAGER_PDF.exists():
        pytest.skip("Roof Manager golden PDF fixture is not available in this checkout")
    analysis = analyze_pdf_geometry(MANAGER_PDF)
    assert analysis["sourceFormat"] == "roof_manager"
    assert analysis["expectedFacetCount"] == 31
    assert analysis["facetCount"] == 21
    assert analysis["geometry"] is None
    area_check = next(
        check
        for check in analysis["validation"]["checks"]
        if check["name"] == "uniform_pitch_sloped_area"
    )
    assert area_check["status"] == "pass"
    assert area_check["differencePercent"] == pytest.approx(0.005, abs=0.001)
    assert analysis["validation"]["summary"]["notEvaluableCount"] == 0
    assert analysis["diagnostics"]["dimensionLabels"]["candidateCount"] == 60
    assert analysis["diagnostics"]["dimensionLabels"]["inlierCount"] == 25
    assert analysis["diagnostics"]["dimensionLabels"]["unmatchedCount"] == 30
    assert analysis["validation"]["scaleHypotheses"]["chosen"] == "sloped"
    assert analysis["status"] == "needs_review"


def test_raster_only_roofr_pages_return_unsupported_without_vector_guessing() -> None:
    with TemporaryDirectory(prefix="roof-raster-test-") as temp:
        path = Path(temp) / "raster.pdf"
        document = fitz.open()
        for text in (
            "Roofr",
            "Diagram",
            "Length measurement report",
            "Area measurement report",
            "Pitch & direction measurement report",
            "Synthetic page 6",
            "Synthetic page 7",
            "Synthetic page 8",
        ):
            page = document.new_page(width=595, height=842)
            page.insert_text((50, 50), text)
        document.save(path)
        document.close()
        analysis = analyze_pdf_geometry(path)
    assert analysis["status"] == "unsupported"
    assert "raster-only" in analysis["reason"]
    assert analysis["geometry"] is None
