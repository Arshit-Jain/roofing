from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.extraction.pipeline import (
    consistency_warnings,
    normalize_report,
    parse_model_json,
    parse_roofr_pdf,
)
from app.schemas import QuantityValue, RawRoofReport
from tests.fakes import FakeFirestore, FakeServerTimestamp


def fake_nvidia(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> list[int]:
    import httpx

    calls: list[int] = []

    def post(*_: object, **__: object) -> object:
        result = responses[len(calls)]
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, int):
            return SimpleNamespace(status_code=result, json=lambda: {})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": result}}]},
        )

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    return calls


def test_malformed_model_json_is_rejected() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_model_json("{broken")


def test_non_object_model_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_model_json("[1, 2]")


def test_raw_report_schema_keeps_missing_measurement_sections_empty() -> None:
    parsed = RawRoofReport.model_validate({})
    assert parsed.project_totals == {}
    assert parsed.measurements == {}


def test_quantity_states_keep_zero_unknown_and_not_surveyed_distinct() -> None:
    assert QuantityValue(state="zero", value=0, unit="ft").state == "zero"
    assert QuantityValue(state="not_surveyed", unit="count").value is None
    assert QuantityValue(state="unknown", unit="count").state == "unknown"
    assert QuantityValue(state="not_reported", unit="count").state == "not_reported"
    with pytest.raises(ValidationError):
        QuantityValue(state="zero", value=1, unit="ft")


def test_sample_fixture_normalizes_and_emits_notes_and_warnings() -> None:
    fixture = json.loads((Path(__file__).parents[3] / "roof_data.json").read_text())
    version = normalize_report(fixture, report_id="r_sample", schema_version="1")
    assert version.values["roofAreaSqFt"].value == 7923
    shingle_reference = next(
        item
        for item in version.materialQuantityReferences
        if item.get("material") == "Field Shingles"
    )
    assert shingle_reference["coveragePerUnit"] == "33.3"
    assert shingle_reference["coverageUnit"] == "sq ft"
    assert version.values["downspouts"].state == "not_surveyed"
    assert any("traced obstructions" in item.lower() for item in version.warnings)
    assert any("Only 5 named area groups" in item for item in version.warnings)
    assert not any(
        "gross roof area" in item.lower() or "eaves plus rakes" in item.lower()
        for item in consistency_warnings(fixture)
    )
    assert not any("with-waste quantity differs" in item for item in version.warnings)


def test_source_with_waste_quantity_mismatch_is_a_warning() -> None:
    fixture = json.loads((Path(__file__).parents[3] / "roof_data.json").read_text())
    eaves = next(row for row in fixture["materials"] if row["material"] == "Drip Edge — Eaves")
    eaves["quantity_with_waste"] = 500
    warnings = consistency_warnings(fixture)
    assert any(
        "drip edge — eaves with-waste quantity differs" in warning.lower() for warning in warnings
    )


def test_fixture_projected_area_check_is_warning_level_when_out_of_tolerance() -> None:
    fixture = json.loads((Path(__file__).parents[3] / "roof_data.json").read_text())
    fixture["measurements"]["slope_multiplier"] = 1.5
    assert any("Projected area" in warning for warning in consistency_warnings(fixture))


def test_normalization_accepts_formatted_values_and_preserves_missing_states() -> None:
    version = normalize_report(
        {
            "project_totals": {"waste_percentage": None},
            "measurements": {"total_roof_area_sf": "7,923 SF"},
            "gutters": {"downspouts": "not surveyed"},
        },
        report_id="r_test",
        schema_version="1",
    )
    assert version.values["roofAreaSqFt"].value == 7923
    assert version.values["downspouts"].state == "not_surveyed"
    assert version.sourceWastePercentage is None
    assert any("waste percentage was not reported" in warning for warning in version.warnings)


def test_roofr_pdf_extracts_mixed_roof_facts_without_gemini() -> None:
    pdf_path = Path(__file__).parents[3] / "83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf"
    extracted = parse_roofr_pdf(pdf_path)
    assert extracted is not None
    raw, raw_text = extracted
    version = normalize_report(raw, report_id="r_roofr", schema_version="2")

    assert raw_text.startswith('{"source_format":"roofr"')
    assert version.sourceFormat == "roofr"
    assert version.totalRoofAreaSqFt == 1158
    assert version.pitchedRoofAreaSqFt == 643
    assert version.flatRoofAreaSqFt == 515
    assert version.roofFacets == 8
    assert version.predominantPitchNotation == "1:12"
    assert len(version.roofPlanes) == 8
    assert sum(plane.roofType == "flat" for plane in version.roofPlanes) == 3
    assert sum(plane.roofType == "pitched" for plane in version.roofPlanes) == 5
    assert all(plane.pitch is None and plane.vertices == [] for plane in version.roofPlanes)
    assert [(item.pitch, item.areaSqFt, item.squares) for item in version.areasByPitch] == [
        ("1:12", Decimal("515"), Decimal("5.2")),
        ("3:12", Decimal("201"), Decimal("2.1")),
        ("6:12", Decimal("443"), Decimal("4.5")),
    ]
    assert version.values["eavesLf"].value == Decimal("98.750000")
    assert version.values["ridgesLf"].value == Decimal("30.750000")
    assert version.values["hipsLf"].value == Decimal("22.666667")
    assert version.values["rakesLf"].value == Decimal("84.583333")
    assert version.values["wallFlashingLf"].value == 18
    assert version.values["stepFlashingLf"].value == Decimal("23.5")
    assert version.values["transitionsLf"].value == Decimal("21.75")
    assert version.values["unspecifiedEdgesLf"].value == Decimal("14.833333")
    recommendations = {item.roofType: {} for item in version.wasteRecommendations}
    for item in version.wasteRecommendations:
        recommendations[item.roofType][item.wastePercentage] = item.sourceAdjustedAreaSqFt
    assert recommendations["pitched"] == {
        Decimal("10"): Decimal("708"),
        Decimal("12"): Decimal("721"),
        Decimal("15"): Decimal("740"),
        Decimal("17"): Decimal("753"),
        Decimal("20"): Decimal("772"),
        Decimal("22"): Decimal("785"),
        Decimal("25"): Decimal("804"),
    }
    assert recommendations["flat"] == {
        Decimal("0"): Decimal("515"),
        Decimal("10"): Decimal("567"),
        Decimal("12"): Decimal("577"),
        Decimal("15"): Decimal("592"),
        Decimal("17"): Decimal("603"),
        Decimal("20"): Decimal("618"),
        Decimal("22"): Decimal("628"),
    }
    cambridge = next(item for item in raw["materials"] if item["material"] == "IKO - Cambridge")
    assert cambridge["unit"] == "bundle"
    assert cambridge["quantityByWastePercentage"] == {
        "10": "22",
        "15": "23",
        "20": "24",
        "25": "25",
    }
    assert "flat roof area is excluded" in cambridge["source"]
    assert version.geometryStatus == "needs_review"
    assert version.sourceWastePercentage is None
    assert any("Rounded pitch-group areas total 1159 SF" in warning for warning in version.warnings)


def test_roofr_pdf_parser_declines_other_reports() -> None:
    pdf_path = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    assert parse_roofr_pdf(pdf_path) is None


def test_mocked_extraction_job_persists_reviewable_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as api
    from app.config import Settings
    from app.services.file_store import store_file_chunks

    repository_root = Path(__file__).parents[3]
    pdf_path = repository_root / "RoofManager-Example-Report.pdf"
    fixture_path = repository_root / "roof_data.json"
    fixture = json.loads(fixture_path.read_text())
    db = FakeFirestore()
    project_id = "project-test"
    report_id = "report-test"
    job_id = "job-test"
    project_ref = db.collection("projects").document(project_id)
    project_ref.set({"ownerUid": "user-test", "status": "processing"})
    report_ref = project_ref.collection("reports").document(report_id)
    with pdf_path.open("rb") as source:
        file_metadata = store_file_chunks(
            db,
            report_ref,
            source,
            chunk_size_bytes=512 * 1024,
            batch_size=8,
            max_file_size_bytes=20 * 1024 * 1024,
        )
    report_ref.set(
        {
            **file_metadata.as_document_fields(),
            "ownerUid": "user-test",
            "status": "complete",
        }
    )
    db.collection("jobs").document(job_id).set({"status": "queued", "attempt": 0})
    monkeypatch.setattr(api, "database", lambda: db)
    monkeypatch.setattr(api, "current_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(api, "_server_time", FakeServerTimestamp)
    monkeypatch.setattr(api, "call_nvidia", lambda *_: (fixture, fixture_path.read_text()))

    api._execute_extraction_job(job_id, project_id, report_id)

    stored_project = project_ref.get().to_dict() or {}
    stored_report = report_ref.get().to_dict() or {}
    stored_job = db.collection("jobs").document(job_id).get().to_dict() or {}
    version_id = stored_project["currentMeasurementVersionId"]
    version = project_ref.collection("measurementVersions").document(version_id).get().to_dict()
    assert stored_project["status"] == "needs_review"
    assert stored_report["extractionStatus"] == "succeeded"
    assert stored_job["status"] == "succeeded"
    assert version is not None
    assert version["values"]["roofAreaSqFt"]["value"] == "7923"


def test_roofr_extraction_job_uses_pdf_parser_without_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as api
    from app.config import Settings
    from app.services.file_store import store_file_chunks

    pdf_path = Path(__file__).parents[3] / "83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf"
    db = FakeFirestore()
    project_id = "roofr-project"
    report_id = "roofr-report"
    job_id = "roofr-job"
    project_ref = db.collection("projects").document(project_id)
    project_ref.set({"ownerUid": "user-test", "status": "processing", "address": None})
    report_ref = project_ref.collection("reports").document(report_id)
    with pdf_path.open("rb") as source:
        file_metadata = store_file_chunks(
            db,
            report_ref,
            source,
            chunk_size_bytes=512 * 1024,
            batch_size=8,
            max_file_size_bytes=20 * 1024 * 1024,
        )
    report_ref.set(
        {**file_metadata.as_document_fields(), "ownerUid": "user-test", "status": "complete"}
    )
    db.collection("jobs").document(job_id).set({"status": "queued", "attempt": 0})
    monkeypatch.setattr(api, "database", lambda: db)
    monkeypatch.setattr(api, "current_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(api, "_server_time", FakeServerTimestamp)
    monkeypatch.setattr(
        api, "call_nvidia", lambda *_: pytest.fail("Roofr PDF should use direct extraction")
    )

    api._execute_extraction_job(job_id, project_id, report_id)

    stored_project = project_ref.get().to_dict() or {}
    version_id = stored_project["currentMeasurementVersionId"]
    version = project_ref.collection("measurementVersions").document(version_id).get().to_dict()
    assert stored_project["address"] == "83 Rue Falardeau, Gatineau, QC J8X 3E1"
    assert version is not None
    assert version["sourceFormat"] == "roofr"
    assert version["flatRoofAreaSqFt"] == "515"
    assert len(version["roofPlanes"]) == 8
    assert version["geometryStatus"] == "needs_review"


def test_raw_model_response_is_persisted_and_reloaded_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as api
    from app.config import Settings

    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-cache")
    project_ref.set({"ownerUid": "user-test"})
    report_ref = project_ref.collection("reports").document("report-cache")
    report = {"ownerUid": "user-test"}
    report_ref.set({**report, "status": "complete"})
    monkeypatch.setattr(api, "_server_time", FakeServerTimestamp)
    settings = Settings(_env_file=None)
    raw = {"source_format": "roof_manager", "project_totals": {}, "measurements": {}}
    raw_text = json.dumps(raw, separators=(",", ":"))

    artifact_id = api._store_raw_extraction(
        db,
        project_ref,
        report_ref,
        report,
        report_ref.id,
        raw_text,
        settings,
    )
    cached = api._load_cached_raw_extraction(project_ref, report_ref.id, settings)

    assert cached == (raw, raw_text, artifact_id)
    stored_report = report_ref.get().to_dict() or {}
    assert stored_report["rawExtractionArtifactId"] == artifact_id
    assert stored_report["rawExtractionPromptVersion"] == settings.extraction_prompt_version


def test_extraction_success_automatically_queues_pdf_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as api

    db = FakeFirestore()
    project_id = "project-auto"
    report_id = "report-auto"
    job_id = "job-auto"
    project_ref = db.collection("projects").document(project_id)
    project_ref.set(
        {"ownerUid": "user-test", "currentReportId": report_id, "status": "processing"}
    )
    report_ref = project_ref.collection("reports").document(report_id)
    report_ref.set({"ownerUid": "user-test", "extractionStatus": "processing"})
    db.collection("jobs").document(job_id).set({"status": "queued", "attempt": 0})
    geometry_runs: list[tuple[str, str, str]] = []

    def complete_extraction(_: str, __: str, ___: str) -> None:
        report_ref.update({"extractionStatus": "succeeded"})
        project_ref.update({"currentMeasurementVersionId": "measurement-auto"})

    monkeypatch.setattr(api, "database", lambda: db)
    monkeypatch.setattr(api, "_server_time", FakeServerTimestamp)
    monkeypatch.setattr(api, "_execute_extraction_job", complete_extraction)
    monkeypatch.setattr(
        api,
        "_run_geometry_reconstruction",
        lambda job, project, report, version: geometry_runs.append((job, report, version)),
    )

    api._process_extraction_job(job_id, project_id, report_id)

    project = project_ref.get().to_dict() or {}
    assert project["geometryStatus"] == "processing"
    assert geometry_runs and geometry_runs[0][1] == report_id


def test_pdf_inspection_rejects_corrupt_encrypted_and_too_many_pages(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    from app.extraction.pipeline import inspect_pdf

    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\ntruncated document")
    with pytest.raises(ValueError, match="could not be opened"):
        inspect_pdf(corrupt, 10)

    document = fitz.open()
    document.new_page()
    encrypted = tmp_path / "encrypted.pdf"
    encrypted.write_bytes(
        document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    )
    document.close()
    with pytest.raises(ValueError, match="Encrypted"):
        inspect_pdf(encrypted, 10)

    sample = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    with pytest.raises(ValueError, match="page count"):
        inspect_pdf(sample, 1)


def test_nvidia_retries_transient_failure_and_parses_valid_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings
    from app.extraction.pipeline import call_nvidia

    class TemporaryProviderError(Exception):
        code = 503

    payload = '{"project_totals": {}, "measurements": {}}'
    calls = fake_nvidia(monkeypatch, [TemporaryProviderError("provider detail"), payload])
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4 sample")
    monkeypatch.setattr("app.extraction.pipeline._pdf_page_metadata", lambda _: [])
    monkeypatch.setattr("app.extraction.pipeline._pdf_image_content_parts", lambda *_: [])
    monkeypatch.setattr("app.extraction.pipeline._detect_source_format", lambda _: "roof_manager")
    settings = Settings(nvidia_api_key="placeholder-test-key", nvidia_max_retries=1)

    result, raw_text = call_nvidia(path, settings)
    assert result["measurements"] == {}
    assert json.loads(raw_text)["source_format"] == "roof_manager"
    assert len(calls) == 2


def test_nvidia_does_not_retry_permanent_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings
    from app.extraction.pipeline import call_nvidia

    class PermanentProviderError(Exception):
        code = 400

    calls = fake_nvidia(monkeypatch, [PermanentProviderError("details are not returned")])
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4 sample")
    monkeypatch.setattr("app.extraction.pipeline._pdf_page_metadata", lambda _: [])
    monkeypatch.setattr("app.extraction.pipeline._pdf_image_content_parts", lambda *_: [])
    settings = Settings(nvidia_api_key="placeholder-test-key", nvidia_max_retries=3)
    with pytest.raises(RuntimeError, match="NVIDIA could not process this report"):
        call_nvidia(path, settings)
    assert len(calls) == 1


def test_nvidia_retries_malformed_output_only_within_configured_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings
    from app.extraction.pipeline import call_nvidia

    payload = '{"project_totals": {}, "measurements": {}}'
    calls = fake_nvidia(monkeypatch, ["not JSON", payload])
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4 sample")
    monkeypatch.setattr("app.extraction.pipeline._pdf_page_metadata", lambda _: [])
    monkeypatch.setattr("app.extraction.pipeline._pdf_image_content_parts", lambda *_: [])
    monkeypatch.setattr("app.extraction.pipeline._detect_source_format", lambda _: "roof_manager")
    settings = Settings(
        nvidia_api_key="placeholder-test-key", nvidia_max_retries=0, malformed_output_retries=1
    )

    result, _ = call_nvidia(path, settings)
    assert result["project_totals"] == {}
    assert len(calls) == 2
