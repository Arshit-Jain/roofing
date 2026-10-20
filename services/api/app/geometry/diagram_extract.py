"""PyMuPDF-only vector and text extraction with explicit page coordinate frames."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.geometry.topology import Point2, Segment2, polygonize_segments


@dataclass(frozen=True)
class VectorWord:
    text: str
    bbox: tuple[float, float, float, float]

    @property
    def center(self) -> Point2:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)


@dataclass(frozen=True)
class VectorSegment:
    page_number: int
    start: Point2
    end: Point2
    drawing_index: int
    item_index: int
    color: tuple[float, float, float] | None
    fill: tuple[float, float, float] | None
    dashes: str | None
    edge_class: str | None = None

    @property
    def length_points(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def segment(self) -> Segment2:
        return self.start, self.end


@dataclass
class VectorPage:
    page_number: int
    width: float
    height: float
    text: str
    words: list[VectorWord]
    text_spans: list[VectorWord]
    segments: list[VectorSegment]
    drawings: list[dict[str, Any]]
    color_legend: dict[str, str]
    item_counts: dict[str, int]
    image_count: int

    @property
    def title(self) -> str:
        lines = [line.strip() for line in self.text.splitlines() if line.strip()]
        for line in lines:
            if (
                "measurement report" in line.lower()
                or "roof area analysis" in line.lower()
                or line.lower()
                in {
                    "diagram",
                    "roof area analysis",
                }
            ):
                return line
        return lines[0] if lines else ""

    @property
    def vector_supported(self) -> bool:
        return bool(self.segments)

    def bounds(
        self, *, edge_classes: set[str] | None = None
    ) -> tuple[float, float, float, float] | None:
        selected = [
            segment
            for segment in self.segments
            if edge_classes is None or segment.edge_class in edge_classes
        ]
        if not selected:
            return None
        xs = [point[0] for segment in selected for point in (segment.start, segment.end)]
        ys = [point[1] for segment in selected for point in (segment.start, segment.end)]
        return min(xs), min(ys), max(xs), max(ys)


def read_vector_page(page: Any, page_number: int) -> VectorPage:
    """Read words and vector paths; coordinates remain in this page's PDF points."""
    words = [
        VectorWord(
            str(word[4]),
            cast(tuple[float, float, float, float], tuple(float(value) for value in word[:4])),
        )
        for word in page.get_text("words")
        if len(word) >= 5
    ]
    text_spans = [
        VectorWord(
            str(span["text"]),
            cast(tuple[float, float, float, float], tuple(float(value) for value in span["bbox"])),
        )
        for block in page.get_text("dict").get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text") and len(span.get("bbox", [])) == 4
    ]
    drawings = page.get_drawings()
    segments: list[VectorSegment] = []
    item_counts: Counter[str] = Counter()
    for drawing_index, drawing in enumerate(drawings):
        color = _color(drawing.get("color"))
        fill = _color(drawing.get("fill"))
        for item_index, item in enumerate(drawing.get("items", [])):
            kind = item[0]
            item_counts[str(kind)] += 1
            for start, end in _item_segments(item):
                if math.dist(start, end) > 0.005:
                    segments.append(
                        VectorSegment(
                            page_number,
                            start,
                            end,
                            drawing_index,
                            item_index,
                            color,
                            fill,
                            drawing.get("dashes"),
                        )
                    )
    vector_page = VectorPage(
        page_number=page_number,
        width=float(page.rect.width),
        height=float(page.rect.height),
        text=page.get_text("text"),
        words=words,
        text_spans=text_spans,
        segments=segments,
        drawings=drawings,
        color_legend={},
        item_counts=dict(item_counts),
        image_count=len(page.get_images(full=True)),
    )
    vector_page.color_legend = extract_page_legend(vector_page)
    if vector_page.color_legend:
        vector_page.segments = [
            VectorSegment(
                segment.page_number,
                segment.start,
                segment.end,
                segment.drawing_index,
                segment.item_index,
                segment.color,
                segment.fill,
                segment.dashes,
                _legend_class(segment.color or segment.fill, vector_page.color_legend),
            )
            for segment in vector_page.segments
        ]
    return vector_page


def extract_page_legend(page: VectorPage) -> dict[str, str]:
    """Read color classes from the page's local swatches and adjacent labels."""
    known_labels = {
        "eave": "eave",
        "eaves": "eave",
        "rake": "rake",
        "rakes": "rake",
        "ridge": "ridge",
        "ridges": "ridge",
        "hip": "hip",
        "hips": "hip",
        "valley": "valley",
        "valleys": "valley",
        "overhang": "overhang",
        "wall flashing": "wall_flashing",
        "step flashing": "step_flashing",
        "curb flashing": "curb_flashing",
        "transition": "transition",
        "transitions": "transition",
        "parapet wall": "parapet",
        "unspecified": "unspecified",
        "main roof": "main_roof",
        "lower eave 1": "lower_eave",
        "hidden overhangs": "hidden_overhang",
        "vents": "vent",
    }
    normalized = [
        (word, re.sub(r"[^a-z0-9]+$", "", word.text.strip().lower())) for word in page.words
    ]
    labels: list[tuple[str, VectorWord]] = []
    for word, token in normalized:
        if token in known_labels:
            labels.append((token, word))
    # PDF text extraction often splits a legend entry into separate words (for
    # example "Wall" + "flashing:"). Join only adjacent words on the same row.
    rows: dict[int, list[VectorWord]] = {}
    for word in page.words:
        rows.setdefault(round(word.center[1] / 3), []).append(word)
    for row in rows.values():
        row.sort(key=lambda word: word.bbox[0])
        for index in range(len(row)):
            for width in (2, 3):
                group = row[index : index + width]
                if len(group) != width:
                    continue
                if any(group[pos + 1].bbox[0] - group[pos].bbox[2] > 6 for pos in range(width - 1)):
                    continue
                phrase = " ".join(
                    re.sub(r"[^a-z0-9]+$", "", word.text.strip().lower()) for word in group
                )
                if phrase in known_labels:
                    labels.append(
                        (
                            phrase,
                            VectorWord(
                                phrase,
                                (
                                    group[0].bbox[0],
                                    group[0].bbox[1],
                                    group[-1].bbox[2],
                                    group[-1].bbox[3],
                                ),
                            ),
                        )
                    )
    legend: dict[str, str] = {}
    class_priority: dict[str, int] = {}
    for label_text, label in labels:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for drawing in page.drawings:
            rect = drawing.get("rect")
            if rect is None:
                continue
            x0, y0, x1, y1 = (float(value) for value in rect)
            swatch_width, swatch_height = x1 - x0, y1 - y0
            # Roof Manager's key uses colored hairline paths; Roofr's key uses
            # small stroked rectangles. Both are local legend evidence.
            is_hairline = 1 <= swatch_width <= 40 and 0 <= swatch_height <= 3
            is_swatch = 1 <= swatch_width <= 40 and 1 <= swatch_height <= 18
            if not (is_hairline or is_swatch):
                continue
            label_x, label_y = label.center
            center_y = (y0 + y1) / 2
            if abs(center_y - label_y) > 9:
                continue
            horizontal_gap = max(label_x - x1, x0 - label_x, 0.0)
            if horizontal_gap > 180:
                continue
            color = drawing.get("fill") or drawing.get("color")
            if color is None:
                continue
            center_x = (x0 + x1) / 2
            candidates.append((math.hypot(label_x - center_x, label_y - center_y), drawing))
        if candidates:
            _, drawing = min(candidates, key=lambda item: item[0])
            color = _color(drawing.get("fill") or drawing.get("color"))
            if color is not None:
                color_key = _color_key(color)
                edge_class = known_labels[label_text]
                priority = 2 if edge_class == "lower_eave" else 1
                if priority >= class_priority.get(color_key, 0):
                    legend[color_key] = edge_class
                    class_priority[color_key] = priority
    return legend


def drawing_segments(drawing: Mapping[str, Any], *, page_number: int = 0) -> list[VectorSegment]:
    color = _color(drawing.get("color"))
    fill = _color(drawing.get("fill"))
    result: list[VectorSegment] = []
    for item_index, item in enumerate(drawing.get("items", [])):
        for start, end in _item_segments(item):
            result.append(
                VectorSegment(
                    page_number,
                    start,
                    end,
                    -1,
                    item_index,
                    color,
                    fill,
                    drawing.get("dashes"),
                )
            )
    return result


def polygon_from_drawing(
    drawing: Mapping[str, Any], *, page_number: int = 0, snap_tolerance: float = 0.8
) -> list[list[Point2]]:
    segments = drawing_segments(drawing, page_number=page_number)
    polygons, _ = polygonize_segments(
        (segment.segment for segment in segments), snap_tolerance=snap_tolerance, minimum_area=4.0
    )
    return polygons


def diagram_segments(
    page: VectorPage,
    *,
    min_y_fraction: float = 0.18,
    max_y_fraction: float = 0.88,
    min_x_fraction: float = 0.08,
    max_x_fraction: float = 0.94,
    require_legend_class: bool = False,
    minimum_length: float = 1.0,
) -> list[VectorSegment]:
    segments = []
    for segment in page.segments:
        center_x = (segment.start[0] + segment.end[0]) / 2
        center_y = (segment.start[1] + segment.end[1]) / 2
        if not (
            page.width * min_x_fraction <= center_x <= page.width * max_x_fraction
            and page.height * min_y_fraction <= center_y <= page.height * max_y_fraction
            and segment.length_points >= minimum_length
        ):
            continue
        if require_legend_class and segment.edge_class is None:
            continue
        segments.append(segment)
    return segments


def numeric_words(
    page: VectorPage,
    *,
    min_y_fraction: float = 0.18,
    max_y_fraction: float = 0.88,
    min_x_fraction: float = 0.08,
    max_x_fraction: float = 0.94,
) -> list[VectorWord]:
    pattern = re.compile(r"^\d{1,4}(?:\.\d+)?$")
    words = [
        word
        for word in page.words
        if pattern.fullmatch(word.text)
        and page.width * min_x_fraction <= word.center[0] <= page.width * max_x_fraction
        and page.height * min_y_fraction <= word.center[1] <= page.height * max_y_fraction
    ]
    # Some vector PDFs duplicate selectable labels in overlapping text spans.
    # Treat identical text and bounding boxes as one observation for calibration.
    unique: dict[tuple[str, int, int, int, int], VectorWord] = {}
    for word in words:
        key: tuple[str, int, int, int, int] = (
            word.text,
            round(word.bbox[0] * 100),
            round(word.bbox[1] * 100),
            round(word.bbox[2] * 100),
            round(word.bbox[3] * 100),
        )
        unique[key] = word
    return sorted(unique.values(), key=lambda word: (word.center[1], word.center[0], word.text))


def analyze_pdf_geometry(
    pdf_path: str | Path,
    *,
    snap_tolerance_points: float = 0.8,
    scale_tolerance_percent: float = 3.0,
    facet_area_tolerance_percent: float = 5.0,
    class_total_tolerance_percent: float = 3.0,
    pitch_tolerance_degrees: float = 1.0,
    height_tolerance_feet: float = 0.05,
    wall_depth_feet: float = 8.0,
) -> dict[str, Any]:
    """Inspect a supported Roofr/Roof Manager PDF without relying on model coordinates."""
    import fitz

    path = Path(pdf_path)
    try:
        document = fitz.open(path)
    except Exception as exc:
        return {
            "status": "unsupported",
            "source": "extracted",
            "sourceFormat": "unknown",
            "reason": "PyMuPDF could not open this source PDF.",
            "errorType": type(exc).__name__,
            "pages": [],
            "facets": [],
            "warnings": [],
        }
    try:
        pages = [read_vector_page(page, index + 1) for index, page in enumerate(document)]
    finally:
        document.close()
    full_text = "\n".join(page.text for page in pages).lower()
    roofr_pages = (
        "diagram",
        "length measurement report",
        "area measurement report",
        "pitch & direction measurement report",
    )
    if "roofr" in full_text and all(title in full_text for title in roofr_pages):
        source_format = "roofr"
    elif "roof area analysis" in full_text and len(pages) == 5:
        source_format = "roof_manager"
    else:
        return {
            "status": "unsupported",
            "source": "extracted",
            "sourceFormat": "unknown",
            "reason": "No supported vector-report adapter matched this PDF.",
            "pages": [_page_summary(page) for page in pages],
            "facets": [],
            "warnings": [],
        }

    from app.geometry.roof_manager import extract_roof_manager
    from app.geometry.roofr import extract_roofr

    settings = {
        "snapTolerancePoints": snap_tolerance_points,
        "scaleTolerancePercent": scale_tolerance_percent,
        "facetAreaTolerancePercent": facet_area_tolerance_percent,
        "classTotalTolerancePercent": class_total_tolerance_percent,
        "pitchToleranceDegrees": pitch_tolerance_degrees,
        "heightToleranceFeet": height_tolerance_feet,
        "wallDepthFeet": wall_depth_feet,
    }
    adapter = extract_roofr if source_format == "roofr" else extract_roof_manager
    result = adapter(pages, settings=settings)
    result["sourceFormat"] = source_format
    result["source"] = "extracted"
    result.setdefault("geometry", None)
    result["pages"] = [_page_summary(page) for page in pages]
    result.setdefault("status", "needs_review")
    result.setdefault("warnings", [])
    return result


def _page_summary(page: VectorPage) -> dict[str, Any]:
    return {
        "pageNumber": page.page_number,
        "widthPoints": round(page.width, 3),
        "heightPoints": round(page.height, 3),
        "title": page.title[:120],
        "wordCount": len(page.words),
        "drawingCount": len(page.drawings),
        "vectorSegmentCount": len(page.segments),
        "itemTypeCounts": page.item_counts,
        "imageCount": page.image_count,
        "vectorSupported": page.vector_supported,
        "colorLegend": page.color_legend,
        "coordinatesArePagePoints": True,
    }


def _item_segments(item: Sequence[Any]) -> list[Segment2]:
    kind = item[0]
    if kind == "l":
        return [(_xy(item[1]), _xy(item[2]))]
    if kind == "c":
        points = [_xy(value) for value in item[1:5]]
        return _flatten_cubic(points, steps=8)
    if kind == "qu":
        quad = item[1]
        try:
            points = [
                _xy(quad.ul),
                _xy(quad.ur),
                _xy(quad.lr),
                _xy(quad.ll),
            ]
        except AttributeError:
            points = [_xy(value) for value in quad]
        return [(points[index], points[(index + 1) % len(points)]) for index in range(len(points))]
    if kind == "re":
        rectangle = item[1]
        x0, y0, x1, y1 = (float(value) for value in rectangle)
        points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(points[index], points[(index + 1) % 4]) for index in range(4)]
    return []


def _flatten_cubic(points: list[Point2], *, steps: int) -> list[Segment2]:
    if len(points) != 4:
        return []
    p0, p1, p2, p3 = points
    sampled: list[Point2] = []
    for index in range(steps + 1):
        t = index / steps
        inverse = 1 - t
        sampled.append(
            (
                inverse**3 * p0[0]
                + 3 * inverse**2 * t * p1[0]
                + 3 * inverse * t**2 * p2[0]
                + t**3 * p3[0],
                inverse**3 * p0[1]
                + 3 * inverse**2 * t * p1[1]
                + 3 * inverse * t**2 * p2[1]
                + t**3 * p3[1],
            )
        )
    return list(zip(sampled, sampled[1:], strict=False))


def _xy(point: Any) -> Point2:
    return float(point.x), float(point.y)


def _color(value: Any) -> tuple[float, float, float] | None:
    if value is None or len(value) < 3:
        return None
    return (
        float(value[0]),
        float(value[1]),
        float(value[2]),
    )


def _color_key(value: Sequence[float]) -> str:
    return "#" + "".join(f"{round(float(component) * 255):02x}" for component in value[:3])


def _legend_class(color: Sequence[float] | None, legend: Mapping[str, str]) -> str | None:
    if color is None:
        return None
    for key, edge_class in legend.items():
        reference = tuple(int(key[index : index + 2], 16) / 255 for index in (1, 3, 5))
        if max(abs(float(color[index]) - reference[index]) for index in range(3)) <= 0.035:
            return edge_class
    return None
