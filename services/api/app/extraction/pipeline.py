"""PDF inspection, NVIDIA vision extraction, and normalized measurement generation."""

from __future__ import annotations

import base64
import json
import math
import random
import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.schemas import (
    MeasurementVersion,
    PitchArea,
    QuantityValue,
    RawRoofReport,
    RoofPlane,
    RoofSystemMeasurement,
    WasteRecommendation,
)

PROMPT = """You extract roofing measurements and diagram points from the attached page images
of a Roofr or Roof Manager measurement PDF. Analyze every page image. Return one JSON object
matching the exact structure below. Use JSON numbers for values, null for missing or unreadable
fields, and include all top-level keys.

Available PDF page numbers, sizes, and titles (1-based): PAGE_METADATA_JSON

{
  "source_format": "unknown",
  "property": {"name": null, "address": null},
  "project_totals": {
    "total_roof_area_sf": null, "predominant_pitch": null,
    "predominant_pitch_degrees": null, "gross_area_sf": null,
    "waste_percentage": null, "ice_water_barrier_sf": null,
    "roof_planes": null, "structures": null
  },
  "measurements": {
    "total_roof_area_sf": null, "pitched_roof_area_sf": null,
    "flat_roof_area_sf": null, "footprint_projected_area_sf": null,
    "gross_area_with_waste_sf": null, "roofing_squares_net": null,
    "roofing_squares_gross": null, "slope_multiplier": null,
    "predominant_pitch": null, "roof_facets": null, "roof_planes": null,
    "ice_water_eave_perimeter_lf": null
  },
  "edge_lengths": {
    "eave_lf": null, "rake_lf": null, "total_perimeter_lf": null,
    "ridge_lf": null, "hip_lf": null, "valley_lf": null,
    "overhang_offset_lf": null, "eaves": null, "rakes": null,
    "hips": null, "ridges": null, "valleys": null, "unspecified": null,
    "hips_+_ridges": null, "eaves_+_rakes": null
  },
  "flashings": {
    "wall_flashing": null, "step_flashing": null,
    "curb_flashing": null, "parapet_wall": null
  },
  "transitions": {"transitions": null},
  "roof_systems": [{"roofType":"pitched","areaSqFt":0,"selectedSystem":null,
    "configurationRequired":true,"source":""}],
  "waste_recommendations": [{"roofType":"pitched","wastePercentage":0,
    "sourceAreaSqFt":0,"sourceAdjustedAreaSqFt":0,"source":""}],
  "areas_by_pitch": [{"pitch":"5:12","areaSqFt":0,"squares":null,"source":""}],
  "roof_planes": [{"id":"","name":"","roofType":"pitched",
    "areaSqFt":null,"pitch":null,"pitchDegrees":null,"vertices":[],"edges":[],
    "materialId":null,"confidence":0.5,"source":""}],
  "roof_sections": [],
  "materials": [],
  "gutters": {"total_gutter_lf": null, "gutter_type": null, "downspouts": null},
  "geometry_review": {
    "status": "needs_review",
    "coordinatePageNumber": null,
    "coordinatePageSize": {"widthPoints": null, "heightPoints": null},
    "coordinateUnits": "PDF points",
    "coordinateOrigin": "top-left",
    "facets": []
  },
  "extraction_notes": []
}

Rules:
1. Copy explicitly reported measurements without changing their values. Preserve source
   units, page references, pitch notation, waste percentages, and distinctions between net
   and waste-adjusted quantities. You may infer missing roof-plane details only when other
   pages in this same PDF provide supporting evidence. Keep such values in geometry_review,
   mark their provenance as inferred, and lower their confidence. Do not invent scalar takeoff
   measurements or use the project name/address as geometric evidence.
2. For each material, return material, quantity, unit, quantity_with_waste,
   unit_with_waste, and based_on when present. Keep Roofr flat and pitched systems separate.
   For roof_planes, roof_systems, waste_recommendations, and areas_by_pitch, emit only rows
   with reported values and include every field shown in their object examples.
3. Preserve aggregate facet counts even when individual facet labels or outlines are unclear.
4. Extract diagram geometry into geometry_review.facets. Use the
   page whose diagram is labelled Area Measurement Report or Roof Area Analysis. Set
   coordinatePageNumber to that 1-based PDF page and copy its exact width and height from the
   supplied page metadata. Coordinates must be PDF points, origin at the page's top-left,
   x increasing right and y increasing down. Do not return pixels or normalized 0-to-1 values.
   If a boundary segment is faint or occluded, infer it only from the same roof outline on
   the diagram, length, pitch/direction, or summary pages, and mark its outline provenance
   as inferred.
5. For each visible facet, return id, name, roofType, areaLabelSqFt, polygonPagePoints,
   pitch, downslopeDirectionPage, edgeTypes, confidence, and provenance. Provenance is an
   object with outline, pitch, downslopeDirection, and area fields, each "reported" or
   "inferred". polygonPagePoints is an ordered
   array of {"x":123,"y":234} objects with at least three visible boundary corners; do not
   repeat the first point at the end. pitch is {"notation":"5:12","rise":5,"run":12}
   or null. The downslope
   direction is a nonzero [dx,dy] vector read from the corresponding pitch/direction diagram,
   or null if the association is unclear. edgeTypes contains one label per polygon edge when
   visible, using eave, rake, ridge, hip, valley, wall_flashing, step_flashing, curb_flashing,
   transition, parapet, unspecified, or other.
6. Never create an unshown facet just to match a reported count. You may complete a partially
   obscured boundary from matching evidence elsewhere in the same report; mark that outline
   inferred and explain uncertainty in extraction_notes. If the facet cannot be associated
   with a roof boundary, return no points. Keep uncertain values low-confidence. All points
   remain candidates for human review, not approved geometry.
7. Explain ambiguous or conflicting values in extraction_notes. Do not combine values from
   different tables unless their meanings are clearly equivalent."""


class ExtractionFailure(RuntimeError):
    """An extraction failure with a message safe to show to an authenticated project owner."""

    def __init__(
        self, code: str, safe_message: str, *, provider_status: int | None = None
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.provider_status = provider_status


def parse_model_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Model output must be a JSON object")
    json.dumps(value, allow_nan=False)
    return value


def inspect_pdf(path: Path, maximum_pages: int) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF inspection dependency is unavailable") from exc
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ValueError("PDF could not be opened") from exc
    with document:
        if document.is_encrypted:
            raise ValueError("Encrypted PDFs are not accepted")
        page_count = len(document)
        if not 1 <= page_count <= maximum_pages:
            raise ValueError("PDF page count is outside the configured limit")
        pages: list[dict[str, Any]] = []
        for page in document:
            page.get_text()
            drawings = page.get_drawings()
            pages.append(
                {
                    "widthPoints": float(page.rect.width),
                    "heightPoints": float(page.rect.height),
                    "textCharacters": len(page.get_text()),
                    "vectorDrawingCount": len(drawings),
                    "imageCount": len(page.get_images(full=True)),
                    "drawingPathSegments": sum(
                        len(drawing.get("items", [])) for drawing in drawings
                    ),
                }
            )
    return {"pageCount": page_count, "pages": pages}


def _pdf_page_metadata(path: Path) -> list[dict[str, Any]]:
    """Give the vision model the PDF coordinate frame for returned diagram points."""
    import fitz

    document = fitz.open(path)
    with document:
        return [
            {
                "pageNumber": index + 1,
                "widthPoints": round(float(page.rect.width), 3),
                "heightPoints": round(float(page.rect.height), 3),
                "title": next(
                    (
                        line.strip()[:120]
                        for line in page.get_text().splitlines()
                        if any(
                            phrase in line.casefold()
                            for phrase in (
                                "diagram",
                                "area measurement report",
                                "roof area analysis",
                                "pitch & direction",
                                "length measurement report",
                            )
                        )
                    ),
                    "",
                ),
            }
            for index, page in enumerate(document)
        ]


def _detect_source_format(path: Path) -> str:
    """Classify the supplied report family without interpreting its measurements."""
    import fitz

    document = fitz.open(path)
    with document:
        text = "\n".join(page.get_text() for page in document).casefold()
    if "prepared by roofr" in text or ("roofr" in text and "area measurement report" in text):
        return "roofr"
    if "roof area analysis" in text or "roof manager" in text:
        return "roof_manager"
    return "unknown"


def _roofr_label_value(text: str, label: str, pattern: str) -> str | None:
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        match = re.fullmatch(rf"{re.escape(label)}\s*:\s*({pattern})", line, flags=re.I)
        if match:
            return match.group(1)
        if line.casefold() == label.casefold() and index + 1 < len(lines):
            candidate = lines[index + 1]
            if re.fullmatch(pattern, candidate, flags=re.I):
                return candidate
    return None


def _roofr_area_value(text: str, label: str) -> Decimal | None:
    value = _roofr_label_value(text, label, r"[\d,]+(?:\.\d+)?\s*sqft")
    return _decimal(value.replace("sqft", "").replace(",", "").strip()) if value else None


def _roofr_length_feet(value: str | None) -> Decimal | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*ft\s*(\d+)\s*in\s*", value, flags=re.I)
    if not match:
        return None
    feet = Decimal(match.group(1))
    inches = Decimal(match.group(2))
    return (feet + inches / Decimal("12")).quantize(Decimal("0.000001"))


def _roofr_lengths(text: str, labels: list[str]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for label in labels:
        raw_value = _roofr_label_value(text, label, r"\d+\s*ft\s*\d+\s*in")
        value = _roofr_length_feet(raw_value)
        if value is not None:
            values[label.casefold().replace(" ", "_")] = value
    return values


def _roofr_pitch_areas(text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.casefold() != "pitch" or index + 3 >= len(lines):
            continue
        pitches = lines[index + 1 : index + 4]
        if not all(re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", item) for item in pitches):
            continue
        try:
            area_index = next(
                idx
                for idx in range(index + 4, min(index + 10, len(lines)))
                if lines[idx].casefold() == "area (sqft)"
            )
        except StopIteration:
            continue
        raw_areas = lines[area_index + 1 : area_index + 4]
        if len(raw_areas) != 3 or not all(
            re.fullmatch(r"[\d,]+(?:\.\d+)?", item) for item in raw_areas
        ):
            continue
        try:
            squares_index = next(
                idx
                for idx in range(area_index + 4, min(area_index + 9, len(lines)))
                if lines[idx].casefold() == "squares"
            )
            raw_squares = lines[squares_index + 1 : squares_index + 4]
        except StopIteration:
            raw_squares = []
        if len(raw_squares) != 3 or not all(
            re.fullmatch(r"\d+(?:\.\d+)?", item) for item in raw_squares
        ):
            raw_squares = []
        result = []
        for index, (pitch, area) in enumerate(zip(pitches, raw_areas, strict=True)):
            numeric_area = Decimal(area.replace(",", ""))
            result.append(
                {
                    "pitch": pitch.replace("/", ":"),
                    "areaSqFt": str(numeric_area),
                    "squares": raw_squares[index] if raw_squares else None,
                    "source": "Roofr report summary; pitch-group areas are display-rounded",
                }
            )
        return result
    return []


def _roofr_waste_values(text: str, roof_type: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = f"{roof_type.title()} waste %"
    area_header = f"{roof_type.title()} area (Sqft)"
    try:
        start = next(idx for idx, item in enumerate(lines) if item.casefold() == header.casefold())
        area_start = next(
            idx
            for idx, item in enumerate(lines[start + 1 :], start + 1)
            if item.casefold() == area_header.casefold()
        )
    except StopIteration:
        return []
    percentages = [
        Decimal(item.rstrip("%"))
        for item in lines[start + 1 : area_start]
        if re.fullmatch(r"\d+(?:\.\d+)?%", item)
    ]
    try:
        squares_start = next(
            idx
            for idx, item in enumerate(lines[area_start + 1 :], area_start + 1)
            if item.casefold() == "squares"
        )
    except StopIteration:
        return []
    areas = [
        Decimal(item.replace(",", ""))
        for item in lines[area_start + 1 : squares_start]
        if re.fullmatch(r"[\d,]+(?:\.\d+)?", item)
    ]
    return [
        {
            "roofType": roof_type,
            "wastePercentage": str(percent),
            "sourceAdjustedAreaSqFt": str(area),
        }
        for percent, area in zip(percentages, areas, strict=False)
    ]


def _roofr_summary_waste_values(text: str) -> list[dict[str, Any]]:
    """Read the compact pitched-waste table used in newer Roofr summaries."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        start = next(
            idx for idx, item in enumerate(lines) if item.casefold() == "recommended waste %"
        )
        area_start = next(
            idx
            for idx, item in enumerate(lines[start + 1 :], start + 1)
            if item.casefold() == "pitched area (sqft)"
        )
        squares_start = next(
            idx
            for idx, item in enumerate(lines[area_start + 1 :], area_start + 1)
            if item.casefold() == "squares"
        )
    except StopIteration:
        return []
    percentages = [
        Decimal(item.rstrip("%"))
        for item in lines[start + 1 : area_start]
        if re.fullmatch(r"\d+(?:\.\d+)?%", item)
    ]
    areas = [
        Decimal(item.replace(",", ""))
        for item in lines[area_start + 1 : squares_start]
        if re.fullmatch(r"[\d,]+(?:\.\d+)?", item)
    ]
    return [
        {
            "roofType": "pitched",
            "wastePercentage": str(percent),
            "sourceAdjustedAreaSqFt": str(area),
        }
        for percent, area in zip(percentages, areas, strict=False)
    ]


def _roofr_material_rows(text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    percentages = [
        Decimal(match.group(1))
        for match in re.finditer(r"Waste\s*\((\d+(?:\.\d+)?)%\)", text, flags=re.I)
    ]
    if len(percentages) != 4:
        return []
    units = {"bundle", "roll", "sheet"}
    rows: list[dict[str, Any]] = []
    for index, unit in enumerate(lines):
        if unit.casefold() not in units or index == 0:
            continue
        name = lines[index - 1]
        if name.casefold() in {"product", "unit"}:
            continue
        raw_quantities = lines[index + 1 : index + 5]
        if len(raw_quantities) != 4 or not all(
            re.fullmatch(r"[\d,]+(?:\.\d+)?", value) for value in raw_quantities
        ):
            continue
        rows.append(
            {
                "material": name,
                "unit": unit,
                "roofType": "pitched",
                "quantityByWastePercentage": {
                    format(percent, "f"): value.replace(",", "")
                    for percent, value in zip(percentages, raw_quantities, strict=True)
                },
                "source": "Roofr material calculations; report says flat roof area is excluded",
            }
        )
    return rows


def _roofr_facet_areas(path: Path, expected_count: int) -> list[dict[str, Any]]:
    """Read display-rounded area labels; vertices and pitch links remain unknown."""
    import fitz

    document = fitz.open(path)
    with document:
        if len(document) < 4:
            return []
        page = document[3]
        words = page.get_text("words")
        drawing_words = [
            word
            for word in words
            if page.rect.width * 0.15 <= word[0] <= page.rect.width * 0.85
            and page.rect.height * 0.25 <= word[1] <= page.rect.height * 0.88
            and re.fullmatch(r"\d+", word[4])
        ]
        flat_words = [
            word
            for word in words
            if word[4].casefold() == "flat"
            and page.rect.width * 0.15 <= word[0] <= page.rect.width * 0.85
            and page.rect.height * 0.22 <= word[1] <= page.rect.height * 0.88
        ]
        facets: list[dict[str, Any]] = []
        for word in sorted(drawing_words, key=lambda item: (item[1], item[0])):
            area = Decimal(word[4])
            is_flat = any(
                0 <= word[1] - flat[1] <= 16 and abs(word[0] - flat[0]) <= 24 for flat in flat_words
            )
            facets.append(
                {
                    "id": f"roofr-facet-{len(facets) + 1:02d}",
                    "name": f"{('Flat' if is_flat else 'Pitched')} source facet {len(facets) + 1}",
                    "roofType": "flat" if is_flat else "pitched",
                    "areaSqFt": str(area),
                    "pitch": None,
                    "pitchDegrees": None,
                    "vertices": [],
                    "edges": [],
                    "materialId": None,
                    "confidence": None,
                    "source": (
                        "Roofr area diagram label; display-rounded; no vertex or pitch "
                        "association validated"
                    ),
                }
            )
        return facets if len(facets) == expected_count else []


def parse_roofr_pdf(path: Path) -> tuple[dict[str, Any], str] | None:
    """Extract Roofr's selectable report tables directly; no model is asked to infer values."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF inspection dependency is unavailable") from exc
    document = fitz.open(path)
    with document:
        pages = [page.get_text() for page in document]
    text = "\n".join(pages)
    if (
        "prepared by roofr" not in text.casefold()
        or "area measurement report" not in text.casefold()
    ):
        return None
    if len(pages) < 7:
        raise ValueError("Roofr report is missing required measurement pages")

    summary = pages[5]
    pitched_area = _roofr_area_value(summary, "Total pitched area")
    flat_area = _roofr_area_value(summary, "Total flat area")
    total_area = _roofr_area_value(summary, "Total roof area")
    facet_match = re.search(r"Total roof facets\s+(\d+)\s+facets", summary, flags=re.I)
    pitch = _roofr_label_value(summary, "Predominant pitch", r"\d+(?:\.\d+)?/\d+(?:\.\d+)?")
    address_match = re.search(
        r"(?m)^\s*(\d{1,6}\s+.+,\s*[A-Z]{2}\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d)\s*$",
        text,
    )
    if total_area is None or pitched_area is None or flat_area is None or facet_match is None:
        raise ValueError("Roofr area summary could not be read")

    edge_values = _roofr_lengths(
        pages[2], ["Eaves", "Valleys", "Hips", "Ridges", "Rakes", "Unspecified"]
    )
    edge_values.update(_roofr_lengths(summary, ["Hips + ridges", "Eaves + rakes"]))
    flashing_values = _roofr_lengths(
        pages[2], ["Wall flashing", "Step flashing", "Curb flashing", "Parapet wall"]
    )
    transition_values = _roofr_lengths(pages[2], ["Transitions"])
    pitch_areas = _roofr_pitch_areas(summary)
    expected_facet_count = int(facet_match.group(1))
    facet_areas = _roofr_facet_areas(path, expected_facet_count)
    if not facet_areas:
        facet_areas = []
    recommendations: list[dict[str, Any]] = []
    for roof_type in ("pitched", "flat"):
        area = pitched_area if roof_type == "pitched" else flat_area
        waste_values = next(
            (
                values
                for page_text in pages
                if (values := _roofr_waste_values(page_text, roof_type))
            ),
            [],
        )
        if not waste_values and roof_type == "pitched":
            waste_values = _roofr_summary_waste_values(summary)
        for item in waste_values:
            item["sourceAreaSqFt"] = str(area)
            item["source"] = (
                "Roofr recommended waste table; reference only, not a selected project waste factor"
            )
            recommendations.append(item)

    areas_by_pitch = pitch_areas
    pitch_area_sum = sum((Decimal(item["areaSqFt"]) for item in areas_by_pitch), Decimal("0"))
    warnings = [
        (
            "Roofr facet labels are rounded for display; use the exact "
            f"{pitched_area} SF pitched and {flat_area} SF flat totals for estimating."
        ),
        (
            "Aggregate pitch groups are not mapped to individual facet IDs; the report does "
            "not provide validated 3D vertices or topology."
        ),
        (
            "Roofr's waste table is based on asphalt shingles for pitched roofing; it is a source "
            "reference, not a universal recommendation or a flat-roof system selection."
        ),
    ]
    one_twelve_area = next(
        (
            Decimal(item["areaSqFt"])
            for item in pitch_areas
            if item["pitch"].replace(":", "/") == "1/12"
        ),
        None,
    )
    if one_twelve_area is not None and one_twelve_area == flat_area:
        pitched_groups = sum(
            (
                Decimal(item["areaSqFt"])
                for item in pitch_areas
                if item["pitch"].replace(":", "/") != "1/12"
            ),
            Decimal("0"),
        )
        if pitched_groups != pitched_area:
            warnings.append(
                "The 1:12 pitch-group area matches the reported flat area while the other "
                "pitch groups total a different area; preserve both source tables."
            )
    if facet_areas and len(facet_areas) != int(facet_match.group(1)):
        warnings.append(
            "The area diagram labels could not be confidently enumerated to the reported "
            "facet count."
        )
    if areas_by_pitch and pitch_area_sum != total_area:
        warnings.append(
            f"Rounded pitch-group areas total {pitch_area_sum} SF while Roofr reports "
            f"{total_area} SF total roof area."
        )

    inspection = inspect_pdf(path, max(len(pages), 1))
    material_page = next(
        (page_text for page_text in pages if "material calculations" in page_text.casefold()),
        "",
    )
    raw: dict[str, Any] = {
        "source_format": "roofr",
        "property": {
            "address": address_match.group(1).strip() if address_match else None,
        },
        "project_totals": {
            "total_roof_area_sf": str(total_area),
            "pitched_roof_area_sf": str(pitched_area),
            "flat_roof_area_sf": str(flat_area),
            "roof_facets": int(facet_match.group(1)),
            "predominant_pitch": pitch,
        },
        "measurements": {
            "total_roof_area_sf": str(total_area),
            "pitched_roof_area_sf": str(pitched_area),
            "flat_roof_area_sf": str(flat_area),
            "roof_facets": int(facet_match.group(1)),
            "predominant_pitch": pitch,
        },
        "edge_lengths": {key: str(value) for key, value in edge_values.items()},
        "flashings": {key: str(value) for key, value in flashing_values.items()},
        "transitions": {key: str(value) for key, value in transition_values.items()},
        "areas_by_pitch": areas_by_pitch,
        "roof_planes": facet_areas,
        "materials": _roofr_material_rows(material_page),
        "roof_systems": [
            {"roofType": "pitched", "areaSqFt": str(pitched_area), "selectedSystem": None},
            {"roofType": "flat", "areaSqFt": str(flat_area), "selectedSystem": None},
        ],
        "waste_recommendations": recommendations,
        "geometry_review": {
            "status": "needs_review",
            "coordinateSource": "Roofr PDF vector drawing",
            "drawingPages": [2, 3, 4, 5],
            "coordinateUnits": "PDF points; not calibrated to feet",
            "scaleEvidence": "No verified scale bar; some displayed lengths are rounded or hidden",
            "pitchAssignmentStatus": "not_validated",
            "edgeConnectivityValidated": False,
            "vectorDrawingCounts": [
                {
                    "page": page_number,
                    "drawings": inspection["pages"][page_number - 1]["vectorDrawingCount"],
                }
                for page_number in (2, 3, 4, 5)
            ],
            "validatedVertices": 0,
            "confidence": None,
        },
        "extraction_notes": warnings,
    }
    return raw, json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def _pdf_image_content_parts(path: Path, dpi: int) -> list[dict[str, Any]]:
    """Render PDF pages to in-memory PNG data URLs for NVIDIA's image chat API."""
    import fitz

    parts: list[dict[str, Any]] = []
    scale = dpi / 72.0
    with fitz.open(path) as document:
        for page_number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            parts.append({"type": "text", "text": f"Report page {page_number}:"})
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
    return parts


def _nvidia_response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _parse_nvidia_report_text(text: str) -> tuple[dict[str, Any], str]:
    """Extract only the final valid report JSON, excluding any reasoning preamble."""
    decoder = json.JSONDecoder()
    closing_thoughts = list(re.finditer(r"</think>\s*", text, flags=re.IGNORECASE))
    sources = [text[closing_thoughts[-1].end() :]] if closing_thoughts else []
    sources.append(text)
    report_keys = {"source_format", "project_totals", "measurements", "geometry_review"}

    for source in sources:
        candidates: list[tuple[dict[str, Any], str]] = []
        for index, character in enumerate(source):
            if character != "{":
                continue
            try:
                value, end = decoder.raw_decode(source, index)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or len(report_keys.intersection(value)) < 2:
                continue
            try:
                RawRoofReport.model_validate(value)
            except ValidationError:
                continue
            candidates.append((value, source[index:end]))
        if candidates:
            raw, raw_json = candidates[-1]
            return raw, raw_json
    raise ValueError("NVIDIA output did not contain a valid report JSON object")


def call_nvidia(pdf_path: Path, settings: Settings) -> tuple[dict[str, Any], str]:
    if not settings.nvidia_api_key:
        raise ExtractionFailure(
            "nvidia_not_configured",
            "NVIDIA extraction is not configured. Set NVIDIA_API_KEY on the API server.",
        )
    try:
        import httpx
    except Exception:
        raise ExtractionFailure(
            "nvidia_client_unavailable",
            "NVIDIA extraction could not start. Check the API dependencies and configuration.",
        ) from None
    try:
        page_metadata = _pdf_page_metadata(pdf_path)
        image_parts = _pdf_image_content_parts(pdf_path, settings.geometry_page_render_dpi)
    except Exception:
        raise ExtractionFailure(
            "nvidia_input_unavailable",
            "The source PDF pages could not be prepared for NVIDIA extraction. Retry the report.",
        ) from None
    prompt = PROMPT.replace(
        "PAGE_METADATA_JSON", json.dumps(page_metadata, separators=(",", ":"))
    )
    content = [{"type": "text", "text": prompt}, *image_parts]
    request_payload = {
        "model": settings.nvidia_model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": settings.nvidia_max_output_tokens,
        "reasoning_budget": settings.nvidia_reasoning_budget,
        "temperature": 0.6,
        "top_p": 0.95,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.nvidia_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    import time

    for malformed_attempt in range(settings.malformed_output_retries + 1):
        response_text: str | None = None
        for attempt in range(settings.nvidia_max_retries + 1):
            try:
                response = httpx.post(
                    settings.nvidia_api_url,
                    headers=headers,
                    json=request_payload,
                    timeout=settings.nvidia_timeout_seconds,
                )
            except Exception as exc:
                status = getattr(exc, "code", getattr(exc, "status_code", None))
                provider_status = (
                    status if isinstance(status, int) and 100 <= status <= 599 else None
                )
                exception_name = type(exc).__name__.casefold()
                retryable = (
                    isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
                    or status in {429, 500, 502, 503, 504}
                    or "timeout" in exception_name
                )
                if not retryable or attempt >= settings.nvidia_max_retries:
                    raise ExtractionFailure(
                        "nvidia_request_failed",
                        "NVIDIA could not process this report. Check the API key and retry.",
                        provider_status=provider_status,
                    ) from None
                time.sleep(min(2**attempt + random.random(), 8))
                continue
            status = response.status_code
            if status < 200 or status >= 300:
                if status not in {429, 500, 502, 503, 504} or attempt >= settings.nvidia_max_retries:
                    raise ExtractionFailure(
                        "nvidia_request_failed",
                        "NVIDIA could not process this report. Check the API key and retry.",
                        provider_status=status,
                    ) from None
                time.sleep(min(2**attempt + random.random(), 8))
                continue
            try:
                response_text = _nvidia_response_text(response.json())
            except ValueError:
                response_text = ""
            break
        try:
            raw, raw_json = _parse_nvidia_report_text(response_text or "")
            if raw.get("source_format") not in {"roofr", "roof_manager"}:
                raw["source_format"] = _detect_source_format(pdf_path)
            RawRoofReport.model_validate(raw)
            raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            return raw, raw_json
        except (ValueError, ValidationError):
            if malformed_attempt >= settings.malformed_output_retries:
                break
    raise ExtractionFailure(
        "nvidia_output_invalid",
        "NVIDIA returned a response that did not match the measurement format. "
        "No results were saved; retry extraction.",
    ) from None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        if value.get("state") == "zero":
            return Decimal("0")
        if value.get("state") not in {None, "measured"}:
            return None
        for key in ("value", "quantity", "amount"):
            if key in value:
                return _decimal(value[key])
        return None
    if isinstance(value, str):
        candidate = value.strip()
        number_match = re.fullmatch(
            r"([+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+))"
            r"\s*(?:%|sf|sq\.?\s*ft|square\s+feet|lf|linear\s+ft|ft|degrees?|°|"
            r"planes?|structures?|bundles?|pieces?|sticks?)?",
            candidate,
            flags=re.IGNORECASE,
        )
        if number_match:
            value = number_match.group(1).replace(",", "")
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except Exception:
        return None


def _quantity(value: Any, unit: str, source: str, not_surveyed: bool = False) -> QuantityValue:
    if not_surveyed:
        return QuantityValue(state="not_surveyed", unit=unit, source=source)
    if isinstance(value, dict) and value.get("state") in {
        "measured",
        "zero",
        "not_surveyed",
        "unknown",
        "not_reported",
    }:
        state = str(value["state"])
        if state == "not_surveyed" or state == "unknown" or state == "not_reported":
            return QuantityValue(state=state, unit=unit, source=source)
    if isinstance(value, str):
        state_text = value.strip().casefold()
        if state_text in {"not surveyed", "not surveyed."}:
            return QuantityValue(state="not_surveyed", unit=unit, source=source)
        if state_text in {"unknown", "unavailable", "not determined"}:
            return QuantityValue(state="unknown", unit=unit, source=source)
        if state_text in {"not reported", "not available", "n/a", "na"}:
            return QuantityValue(state="not_reported", unit=unit, source=source)
    numeric = _decimal(value)
    if numeric is None or numeric < 0:
        return QuantityValue(state="not_reported", unit=unit, source=source)
    if numeric == 0:
        return QuantityValue(state="zero", value=Decimal("0"), unit=unit, source=source)
    return QuantityValue(state="measured", value=numeric, unit=unit, source=source)


def consistency_warnings(raw: dict[str, Any], tolerance: Decimal = Decimal("0.02")) -> list[str]:
    measurements = raw.get("measurements", {})
    edges = raw.get("edge_lengths", {})
    totals = raw.get("project_totals", {})
    warnings: list[str] = []
    area = _decimal(measurements.get("total_roof_area_sf", totals.get("total_roof_area_sf")))
    gross = _decimal(measurements.get("gross_area_with_waste_sf", totals.get("gross_area_sf")))
    waste = _decimal(totals.get("waste_percentage"))
    if area is not None and gross is not None and waste is not None:
        expected = area * (Decimal("1") + waste / Decimal("100"))
        if abs(expected - gross) > Decimal("1") + tolerance * max(expected, Decimal(1)):
            warnings.append("Reported gross roof area differs from net area plus reported waste")
    eaves = _decimal(edges.get("eave_lf"))
    rakes = _decimal(edges.get("rake_lf"))
    perimeter = _decimal(edges.get("total_perimeter_lf"))
    if (
        eaves is not None
        and rakes is not None
        and perimeter is not None
        and abs(eaves + rakes - perimeter) > Decimal("1")
    ):
        warnings.append("Eaves plus rakes differs from reported perimeter")
    projected = _decimal(measurements.get("footprint_projected_area_sf"))
    multiplier = _decimal(measurements.get("slope_multiplier"))
    if area is not None and projected is not None and multiplier is not None and projected > 0:
        estimated = projected * multiplier
        if abs(estimated - area) > max(Decimal("25"), tolerance * area):
            warnings.append(
                "Projected area and predominant slope multiplier do not explain total roof area"
            )
    pitch = str(measurements.get("predominant_pitch", totals.get("predominant_pitch", "")))
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)\s*", pitch)
    if match and multiplier is not None:
        rise, run = Decimal(match.group(1)), Decimal(match.group(2))
        expected_multiplier = Decimal(str(math.sqrt(float(rise**2 + run**2) / float(run**2))))
        if abs(expected_multiplier - multiplier) > Decimal("0.005"):
            warnings.append("Reported slope multiplier differs from predominant pitch calculation")
    net_squares = _decimal(measurements.get("roofing_squares_net"))
    if area is not None and net_squares is not None:
        calculated = (area / Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if abs(calculated - net_squares) > Decimal("0.1"):
            warnings.append("Reported net roofing squares differ from net area divided by 100")
    gross_squares = _decimal(measurements.get("roofing_squares_gross"))
    if gross is not None and gross_squares is not None:
        calculated = (gross / Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if abs(calculated - gross_squares) > Decimal("0.1"):
            warnings.append("Reported gross roofing squares differ from gross area divided by 100")
    materials = raw.get("materials", [])
    named_materials = {
        str(row.get("material", "")).casefold(): row for row in materials if isinstance(row, dict)
    }
    if waste is not None:
        for name, row in named_materials.items():
            net_quantity = _decimal(row.get("quantity"))
            gross_quantity = _decimal(row.get("quantity_with_waste"))
            net_unit = str(row.get("unit", "")).strip().casefold()
            gross_unit = str(row.get("unit_with_waste") or row.get("unit", "")).strip().casefold()
            area_units = {"sf", "sq ft", "sqft"}
            length_units = {"lf", "ft", "linear ft"}
            if (
                net_quantity is None
                or gross_quantity is None
                or net_quantity < 0
                or gross_quantity < 0
            ):
                continue
            if net_unit != gross_unit or (
                net_unit not in area_units and net_unit not in length_units
            ):
                continue
            expected_gross = (net_quantity * (Decimal("1") + waste / Decimal("100"))).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            if abs(expected_gross - gross_quantity) > Decimal("1"):
                warnings.append(
                    f"Reported {name} with-waste quantity differs from net quantity "
                    "plus reported waste"
                )
    ridge_cap = next((row for name, row in named_materials.items() if "ridge cap" in name), None)
    ridge = _decimal(edges.get("ridge_lf"))
    hip = _decimal(edges.get("hip_lf"))
    if ridge is not None and hip is not None and ridge_cap:
        reported = _decimal(ridge_cap.get("quantity"))
        if reported is not None and abs(ridge + hip - reported) > Decimal("1"):
            warnings.append("Ridge plus hip length differs from net ridge cap quantity")
    if area is not None and gross is not None and waste is not None:
        expected_gross = (area * (Decimal("1") + waste / Decimal("100"))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        if abs(expected_gross - gross) > Decimal("1"):
            warnings.append("Rounded net area plus source waste differs from reported gross area")
    shingles = next((row for name, row in named_materials.items() if "field shingle" in name), None)
    if area is not None and shingles:
        basis = str(shingles.get("based_on", ""))
        coverage_match = re.search(r"1\s*bundle\s*=\s*([0-9.]+)\s*sq\s*ft", basis, flags=re.I)
        if coverage_match:
            coverage = Decimal(coverage_match.group(1))
            expected_net_units = (area / coverage).to_integral_value(rounding=ROUND_CEILING)
            reported_net_units = _decimal(shingles.get("quantity"))
            if reported_net_units is not None and abs(
                expected_net_units - reported_net_units
            ) > Decimal("1"):
                warnings.append(
                    "Reported shingle purchase units differ from net area and bundle coverage"
                )
            if gross is not None:
                expected_gross_units = (gross / coverage).to_integral_value(rounding=ROUND_CEILING)
                reported_gross_units = _decimal(shingles.get("quantity_with_waste"))
                if reported_gross_units is not None and abs(
                    expected_gross_units - reported_gross_units
                ) > Decimal("1"):
                    warnings.append(
                        "Reported shingle purchase units differ from gross area and bundle coverage"
                    )
    named_groups = raw.get("roof_planes")
    reported_count = _decimal(measurements.get("roof_planes", totals.get("roof_planes")))
    if (
        isinstance(named_groups, list)
        and reported_count is not None
        and len(named_groups) < reported_count
    ):
        warnings.append(
            f"Only {len(named_groups)} named area groups are listed for "
            f"{reported_count} reported planes; they are not the full plane topology"
        )
    return warnings


def _safe_geometry_review(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep model geometry finite and Firestore-safe before it enters a measurement doc."""
    allowed_edges = {
        "eave",
        "rake",
        "ridge",
        "hip",
        "valley",
        "wall_flashing",
        "step_flashing",
        "curb_flashing",
        "transition",
        "unspecified",
        "parapet",
        "other",
    }

    def finite_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    page_number = raw.get("coordinatePageNumber")
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        page_number = None
    raw_size = raw.get("coordinatePageSize")
    size: dict[str, float] = {}
    if isinstance(raw_size, dict):
        for key in ("widthPoints", "heightPoints"):
            number = finite_number(raw_size.get(key))
            if number is not None and number > 0:
                size[key] = number

    facets: list[dict[str, Any]] = []
    warnings: list[str] = []
    provenance_fields = ("outline", "pitch", "downslopeDirection", "area")
    raw_facets = raw.get("facets")
    for index, item in enumerate(raw_facets if isinstance(raw_facets, list) else [], start=1):
        if not isinstance(item, dict):
            warnings.append(f"NVIDIA facet {index} was omitted because it was not an object.")
            continue
        raw_points = item.get("polygonPagePoints")
        if not isinstance(raw_points, list) or not 3 <= len(raw_points) <= 100:
            warnings.append(f"NVIDIA facet {index} has no usable polygon point list.")
            continue
        points: list[dict[str, float]] = []
        for raw_point in raw_points:
            if isinstance(raw_point, dict):
                x = finite_number(raw_point.get("x"))
                y = finite_number(raw_point.get("y"))
            elif isinstance(raw_point, (list, tuple)) and len(raw_point) == 2:
                x = finite_number(raw_point[0])
                y = finite_number(raw_point[1])
            else:
                x = y = None
            if x is None or y is None:
                points = []
                break
            points.append({"x": x, "y": y})
        if len(points) < 3:
            warnings.append(f"NVIDIA facet {index} has invalid or non-finite coordinates.")
            continue

        pitch: dict[str, Any] | None = None
        raw_pitch = item.get("pitch")
        if isinstance(raw_pitch, dict):
            rise = finite_number(raw_pitch.get("rise"))
            run = finite_number(raw_pitch.get("run"))
            if rise is not None and run is not None and 0 <= rise <= 36 and 0 < run <= 36:
                pitch = {
                    "notation": str(raw_pitch.get("notation") or f"{rise:g}:{run:g}")[:20],
                    "rise": rise,
                    "run": run,
                }
        direction: list[float] | None = None
        raw_direction = item.get("downslopeDirectionPage")
        if isinstance(raw_direction, (list, tuple)) and len(raw_direction) == 2:
            dx = finite_number(raw_direction[0])
            dy = finite_number(raw_direction[1])
            if dx is not None and dy is not None and math.hypot(dx, dy) > 1e-8:
                direction = [dx, dy]
        raw_edges = item.get("edgeTypes")
        edge_types = (
            [str(edge) for edge in raw_edges if str(edge) in allowed_edges]
            if isinstance(raw_edges, list)
            else []
        )
        if len(edge_types) != len(points):
            edge_types = []
        area = finite_number(item.get("areaLabelSqFt"))
        confidence = finite_number(item.get("confidence"))
        raw_provenance = item.get("provenance")
        provenance: dict[str, str] = {}
        for field in provenance_fields:
            value = raw_provenance.get(field) if isinstance(raw_provenance, dict) else None
            if value not in {"reported", "inferred"}:
                has_evidence = {
                    "outline": bool(points),
                    "pitch": pitch is not None,
                    "downslopeDirection": direction is not None,
                    "area": area is not None,
                }[field]
                value = "reported" if has_evidence else "inferred"
            provenance[field] = value
        facets.append(
            {
                "id": str(item.get("id") or f"nvidia-facet-{index:03d}")[:100],
                "name": str(item.get("name") or f"NVIDIA facet {index}")[:120],
                "roofType": item.get("roofType")
                if item.get("roofType") in {"flat", "pitched"}
                else "pitched",
                "areaLabelSqFt": area if area is not None and area >= 0 else None,
                "polygonPagePoints": points,
                "pitch": pitch,
                "downslopeDirectionPage": direction,
                "edgeTypes": edge_types,
                "confidence": min(max(confidence, 0.0), 1.0) if confidence is not None else 0.5,
                "provenance": provenance,
            }
        )
    expected = raw.get("expectedFacetCount")
    expected_count = (
        expected
        if isinstance(expected, int) and not isinstance(expected, bool) and expected >= 0
        else None
    )
    return {
        "status": "needs_review",
        "source": "nvidia_visual_extraction",
        "coordinatePageNumber": page_number,
        "coordinatePageSize": size,
        "coordinateUnits": "PDF points",
        "coordinateOrigin": "top-left",
        "expectedFacetCount": expected_count,
        "facets": facets,
        "warnings": warnings,
    }


def _normalized_roof_planes(
    rows: list[dict[str, Any]], geometry_evidence: dict[str, Any]
) -> tuple[list[RoofPlane], list[str]]:
    planes: list[RoofPlane] = []
    warnings: list[str] = []
    for index, item in enumerate(rows, start=1):
        try:
            planes.append(RoofPlane.model_validate(item))
        except ValidationError:
            warnings.append(f"NVIDIA roof-plane row {index} was omitted because it was incomplete.")
    if planes:
        return planes, warnings
    for index, facet in enumerate(geometry_evidence.get("facets", []), start=1):
        if not isinstance(facet, dict):
            continue
        raw_pitch = facet.get("pitch")
        pitch = raw_pitch.get("notation") if isinstance(raw_pitch, dict) else None
        try:
            planes.append(
                RoofPlane(
                    id=str(facet.get("id") or f"nvidia-facet-{index:03d}"),
                    name=str(facet.get("name") or f"NVIDIA facet {index}"),
                    roofType=facet.get("roofType", "pitched"),
                    areaSqFt=facet.get("areaLabelSqFt"),
                    pitch=pitch,
                    vertices=facet.get("polygonPagePoints", []),
                    edges=facet.get("edgeTypes", []),
                    confidence=facet.get("confidence"),
                    source="NVIDIA visual extraction; requires geometry review",
                )
            )
        except ValidationError:
            warnings.append(f"NVIDIA facet {index} could not be represented as a roof-plane row.")
    return planes, warnings


def normalize_report(
    raw: dict[str, Any], *, report_id: str, schema_version: str
) -> MeasurementVersion:
    validated = RawRoofReport.model_validate(raw)
    if validated.source_format == "roofr":
        return normalize_roofr_report(raw, report_id=report_id, schema_version=schema_version)
    measurements = validated.measurements
    totals = validated.project_totals
    edges = validated.edge_lengths
    notes = validated.extraction_notes
    downspouts_value = validated.gutters.get("downspouts")
    downspouts_unreported = any(
        "not surveyed" in note.lower() and "downspout" in note.lower() for note in notes
    ) or (isinstance(downspouts_value, str) and "not surveyed" in downspouts_value.lower())
    mapping: dict[str, tuple[Any, str]] = {
        "roofAreaSqFt": (
            measurements.get("total_roof_area_sf", totals.get("total_roof_area_sf")),
            "sq ft",
        ),
        "sourceWastePercentage": (totals.get("waste_percentage"), "%"),
        "projectedAreaSqFt": (measurements.get("footprint_projected_area_sf"), "sq ft"),
        "grossAreaSqFt": (
            measurements.get("gross_area_with_waste_sf", totals.get("gross_area_sf")),
            "sq ft",
        ),
        "predominantPitchDegrees": (totals.get("predominant_pitch_degrees"), "degrees"),
        "roofPlaneCount": (measurements.get("roof_planes", totals.get("roof_planes")), "planes"),
        "structureCount": (totals.get("structures"), "structures"),
        "iceWaterBarrierSqFt": (totals.get("ice_water_barrier_sf"), "sq ft"),
        "eavesLf": (edges.get("eave_lf"), "ft"),
        "rakesLf": (edges.get("rake_lf"), "ft"),
        "ridgeLf": (edges.get("ridge_lf"), "ft"),
        "hipsLf": (edges.get("hip_lf"), "ft"),
        "valleysLf": (edges.get("valley_lf"), "ft"),
        "overhangOffsetLf": (edges.get("overhang_offset_lf"), "ft"),
        "downspouts": (downspouts_value, "count"),
    }
    values: dict[str, QuantityValue] = {}
    for key, (value, unit) in mapping.items():
        values[key] = _quantity(
            value, unit, "Roof Manager report", key == "downspouts" and downspouts_unreported
        )
    warnings = consistency_warnings(raw)
    warnings.extend(notes)
    geometry_evidence = _safe_geometry_review(validated.geometry_review)
    warnings.extend(geometry_evidence["warnings"])
    roof_planes, plane_warnings = _normalized_roof_planes(
        validated.roof_planes, geometry_evidence
    )
    warnings.extend(plane_warnings)
    waste = _decimal(totals.get("waste_percentage"))
    if waste is None:
        warnings.append(
            "Source waste percentage was not reported; configure waste before estimating"
        )
    provenance = {
        name: {"original": value.model_dump(mode="json"), "corrected": None}
        for name, value in values.items()
    }
    material_references: list[dict[str, Any]] = []
    for material in validated.materials:
        reference = dict(material)
        based_on = str(reference.get("based_on", ""))
        coverage_match = re.search(
            r"1\s*bundle\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*sq\s*ft", based_on, flags=re.I
        )
        if coverage_match:
            reference["coveragePerUnit"] = coverage_match.group(1)
            reference["coverageUnit"] = "sq ft"
            reference["coveragePurchaseUnit"] = "bundle"
        material_references.append(reference)
    return MeasurementVersion(
        sourceReportId=report_id,
        schemaVersion=schema_version,
        origin="extracted",
        sourceWastePercentage=waste,
        predominantPitchNotation=str(
            measurements.get("predominant_pitch", totals.get("predominant_pitch"))
        )
        if measurements.get("predominant_pitch", totals.get("predominant_pitch"))
        else None,
        values=values,
        materialQuantityReferences=material_references,
        fieldProvenance=provenance,
        warnings=warnings,
        sourceFormat=validated.source_format or "roof_manager",
        roofPlanes=roof_planes,
        geometryStatus="needs_review",
        geometryEvidence=geometry_evidence,
        sourceMeasurements=values,
    )


def normalize_roofr_report(
    raw: dict[str, Any], *, report_id: str, schema_version: str
) -> MeasurementVersion:
    validated = RawRoofReport.model_validate(raw)
    totals = validated.project_totals
    measurements = validated.measurements
    source = "Roofr measurement report"
    specifications: dict[str, tuple[Any, str]] = {
        "roofAreaSqFt": (measurements.get("total_roof_area_sf"), "sq ft"),
        "pitchedRoofAreaSqFt": (measurements.get("pitched_roof_area_sf"), "sq ft"),
        "flatRoofAreaSqFt": (measurements.get("flat_roof_area_sf"), "sq ft"),
        "roofPlaneCount": (measurements.get("roof_facets"), "facets"),
    }
    edge_key_map = {
        "eaves": ("eavesLf", "ft"),
        "valleys": ("valleysLf", "ft"),
        "hips": ("hipsLf", "ft"),
        "ridges": ("ridgesLf", "ft"),
        "rakes": ("rakesLf", "ft"),
        "unspecified": ("unspecifiedEdgesLf", "ft"),
        "hips_+_ridges": ("hipsAndRidgesLf", "ft"),
        "eaves_+_rakes": ("eavesAndRakesLf", "ft"),
    }
    flashing_key_map = {
        "wall_flashing": ("wallFlashingLf", "ft"),
        "step_flashing": ("stepFlashingLf", "ft"),
        "curb_flashing": ("curbFlashingLf", "ft"),
        "parapet_wall": ("parapetWallLf", "ft"),
    }
    for source_key, (target_key, unit) in edge_key_map.items():
        specifications[target_key] = (validated.edge_lengths.get(source_key), unit)
    for source_key, (target_key, unit) in flashing_key_map.items():
        specifications[target_key] = (validated.flashings.get(source_key), unit)
    specifications["transitionsLf"] = (validated.transitions.get("transitions"), "ft")

    values = {key: _quantity(value, unit, source) for key, (value, unit) in specifications.items()}
    predominant_pitch = measurements.get("predominant_pitch", totals.get("predominant_pitch"))
    pitch_notation = str(predominant_pitch).replace("/", ":") if predominant_pitch else None
    provenance = {
        name: {"original": value.model_dump(mode="json"), "corrected": None}
        for name, value in values.items()
    }
    source_measurements = dict(values)
    geometry_evidence = _safe_geometry_review(validated.geometry_review)
    warnings = list(validated.extraction_notes)
    warnings.extend(geometry_evidence["warnings"])
    pitch_area_sum = sum(
        (_decimal(item.get("areaSqFt")) or Decimal("0") for item in validated.areas_by_pitch),
        Decimal("0"),
    )
    total_area = _decimal(measurements.get("total_roof_area_sf"))
    if (
        total_area is not None
        and validated.areas_by_pitch
        and abs(pitch_area_sum - total_area) > Decimal("1")
    ):
        warnings.append(
            f"Aggregate pitch-group areas total {pitch_area_sum} SF versus {total_area} SF total; "
            "values are rounded and kept separate from flat/pitched system totals."
        )
    if not predominant_pitch:
        warnings.append(
            "Predominant pitch was not reported; individual plane pitches remain unassigned."
        )

    roof_systems = [
        RoofSystemMeasurement(
            roofType=str(item.get("roofType")),
            areaSqFt=Decimal(str(item.get("areaSqFt", "0"))),
            selectedSystem=None,
            configurationRequired=True,
            source=source,
        )
        for item in validated.roof_systems
    ]
    waste_recommendations = [
        WasteRecommendation(
            roofType=str(item.get("roofType")),
            wastePercentage=Decimal(str(item.get("wastePercentage", "0"))),
            sourceAreaSqFt=Decimal(str(item.get("sourceAreaSqFt", "0"))),
            sourceAdjustedAreaSqFt=Decimal(str(item.get("sourceAdjustedAreaSqFt", "0"))),
            source=str(item.get("source", source)),
        )
        for item in validated.waste_recommendations
    ]
    roof_planes, plane_warnings = _normalized_roof_planes(
        validated.roof_planes, geometry_evidence
    )
    warnings.extend(plane_warnings)
    def quantity_map(
        rows: dict[str, Any], mapping: dict[str, tuple[str, str]]
    ) -> dict[str, QuantityValue]:
        return {
            output: _quantity(rows.get(key), unit, source)
            for key, (output, unit) in mapping.items()
        }

    edge_lengths = quantity_map(
        validated.edge_lengths,
        {key: (key, "ft") for key in edge_key_map},
    )
    flashings = quantity_map(validated.flashings, {key: (key, "ft") for key in flashing_key_map})
    transitions = quantity_map(validated.transitions, {"transitions": ("transitions", "ft")})
    areas_by_pitch = [PitchArea.model_validate(item) for item in validated.areas_by_pitch]
    source_waste = _decimal(totals.get("waste_percentage"))

    return MeasurementVersion(
        sourceReportId=report_id,
        schemaVersion=schema_version,
        origin="extracted",
        sourceWastePercentage=source_waste,
        predominantPitchNotation=pitch_notation,
        values=values,
        materialQuantityReferences=validated.materials,
        fieldProvenance=provenance,
        warnings=warnings,
        sourceFormat="roofr",
        totalRoofAreaSqFt=total_area,
        pitchedRoofAreaSqFt=_decimal(measurements.get("pitched_roof_area_sf")),
        flatRoofAreaSqFt=_decimal(measurements.get("flat_roof_area_sf")),
        roofFacets=int(_decimal(measurements.get("roof_facets")) or 0),
        areasByPitch=areas_by_pitch,
        roofPlanes=roof_planes,
        roofSystems=roof_systems,
        edgeLengths=edge_lengths,
        flashings=flashings,
        transitions=transitions,
        wasteRecommendations=waste_recommendations,
        sourceMeasurements=source_measurements,
        correctedMeasurements={},
        geometryStatus="needs_review",
        geometryEvidence=geometry_evidence,
    )
