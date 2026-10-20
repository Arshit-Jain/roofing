"""Forward-only migrations for persisted geometry payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def migrate_geometry(value: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy geometry to v1.2's per-plane provenance metadata."""
    geometry = deepcopy(value)
    schema_version = geometry.get("schemaVersion")
    if schema_version == "1.2":
        return geometry
    if schema_version not in {"1.0", "1.1"}:
        raise ValueError("Unsupported geometry schema version")
    planes = geometry.get("planes")
    if not isinstance(planes, list):
        raise ValueError(f"Geometry schema {schema_version} is missing planes")
    for plane in planes:
        if not isinstance(plane, dict):
            raise ValueError(f"Geometry schema {schema_version} contains an invalid plane")
        area = plane.get("reportedAreaSqFt")
        if area is not None and (
            isinstance(area, bool) or not isinstance(area, (int, float)) or area < 0
        ):
            raise ValueError(f"Geometry schema {schema_version} contains an invalid report area")
        if schema_version == "1.0":
            plane["reportedAreaSqFt"] = float(area) if area is not None else None
        if not isinstance(plane.get("provenance"), dict):
            plane["provenance"] = {
                "outline": "reported",
                "pitch": "reported",
                "downslopeDirection": "reported",
                "area": "reported",
            }
        confidence = plane.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = {
                "high": 0.95,
                "medium": 0.75,
                "low": 0.5,
                "illustrative": 0.5,
            }.get(str(geometry.get("confidence", "low")), 0.5)
        plane["confidence"] = min(max(float(confidence), 0.0), 1.0)
    geometry["schemaVersion"] = "1.2"
    return geometry
