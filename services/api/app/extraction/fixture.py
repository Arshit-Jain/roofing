"""Normalize the checked-in extraction fixture without making a paid model call."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import get_settings
from app.extraction.pipeline import normalize_report


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    fixture = json.loads((root / "roof_data.json").read_text(encoding="utf-8"))
    version = normalize_report(
        fixture, report_id="fixture_report", schema_version=get_settings().extraction_schema_version
    )
    print(json.dumps(version.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
