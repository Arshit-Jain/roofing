from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.firebase import get_current_user
from tests.fakes import FakeFirestore, FakeServerTimestamp

_DEFAULT_SERVER_TIME = main._server_time
_DEFAULT_DATABASE = main.database


def client_for(db: FakeFirestore, uid: str = "user-a") -> TestClient:
    main.app.dependency_overrides[_DEFAULT_DATABASE] = lambda: db
    main.app.dependency_overrides[get_current_user] = lambda: {
        "uid": uid,
        "email": f"{uid}@example.test",
    }
    main._server_time = lambda: FakeServerTimestamp()
    return TestClient(main.app)


def reset_overrides() -> None:
    main.app.dependency_overrides.clear()
    main._server_time = _DEFAULT_SERVER_TIME


def test_missing_auth_token_is_rejected() -> None:
    reset_overrides()
    response = TestClient(main.app).get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


def test_invalid_and_expired_tokens_are_rejected(monkeypatch) -> None:
    from fastapi import HTTPException

    from app import firebase

    def reject_token(_: str, settings=None):
        del settings
        raise HTTPException(
            status_code=401, detail={"code": "invalid_token", "message": "Sign-in expired."}
        )

    monkeypatch.setattr(firebase, "verify_id_token", reject_token)
    response = TestClient(main.app).get("/api/me", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_project_creation_and_other_user_is_hidden() -> None:
    db = FakeFirestore()
    client = client_for(db)
    response = client.post("/api/projects", json={"name": "Maple Street roof"})
    assert response.status_code == 201
    assert response.json()["createdAt"] == "2026-01-01T00:00:00Z"
    assert response.json()["updatedAt"] == "2026-01-01T00:00:00Z"
    project_id = response.json()["id"]
    reset_overrides()
    other_user = client_for(db, "user-b")
    assert other_user.get(f"/api/projects/{project_id}").status_code == 404
    assert (
        other_user.patch(f"/api/projects/{project_id}", json={"name": "stolen"}).status_code == 404
    )
    assert other_user.delete(f"/api/projects/{project_id}").status_code == 404
    reset_overrides()


def test_cross_user_report_estimate_and_job_endpoints_return_404() -> None:
    db = FakeFirestore()
    db.collection("projects").document("project-a").set(
        {"ownerUid": "user-a", "status": "uploaded"}
    )
    project_ref = db.collection("projects").document("project-a")
    project_ref.collection("reports").document("report-a").set(
        {
            "ownerUid": "user-a",
            "status": "complete",
            "chunkCount": 1,
            "fileSizeBytes": 1,
            "sha256": "x",
        }
    )
    project_ref.collection("estimates").document("estimate-a").set({"ownerUid": "user-a"})
    db.collection("jobs").document("job-a").set({"ownerUid": "user-a", "projectId": "project-a"})
    db.collection("users").document("user-a").collection("materialCatalog").document(
        "material-a"
    ).set({"name": "Shingles"})
    client = client_for(db, "user-b")
    paths = [
        ("GET", "/api/projects/project-a/reports"),
        ("GET", "/api/projects/project-a/reports/report-a/file"),
        ("DELETE", "/api/projects/project-a/reports/report-a"),
        ("POST", "/api/projects/project-a/reports/report-a/extract"),
        ("GET", "/api/projects/project-a/measurements"),
        ("PUT", "/api/projects/project-a/measurements"),
        ("POST", "/api/projects/project-a/estimates"),
        ("GET", "/api/projects/project-a/estimates"),
        ("GET", "/api/projects/project-a/estimates/estimate-a"),
        ("GET", "/api/jobs/job-a"),
        ("POST", "/api/projects/project-a/reports"),
        ("PATCH", "/api/materials/material-a"),
        ("DELETE", "/api/materials/material-a"),
    ]
    for method, path in paths:
        if path.endswith("/measurements") and method == "PUT":
            body = {"values": {}, "sourceWastePercentage": "8"}
        elif path.endswith("/estimates") and method == "POST":
            body = {
                "lines": [
                    {"description": "Materials", "quantity": "1", "unit": "unit", "unitPrice": "0"}
                ]
            }
        elif path == "/api/materials/material-a" and method == "PATCH":
            body = {
                "name": "Shingles",
                "category": "roofing",
                "unit": "bundle",
                "pricePerUnit": "0",
            }
        else:
            body = {} if method in {"POST", "PUT"} else None
        response = client.request(method, path, json=body)
        assert response.status_code == 404, (method, path, response.text)
    assert client.get("/api/materials").json() == []
    reset_overrides()


def test_cross_user_geometry_endpoints_return_404() -> None:
    db = FakeFirestore()
    project = db.collection("projects").document("project-geometry")
    project.set(
        {
            "ownerUid": "user-a",
            "currentReportId": "report-geometry",
            "currentGeometryVersionId": "geometry-a",
        }
    )
    project.collection("reports").document("report-geometry").set(
        {"ownerUid": "user-a", "status": "complete", "chunkCount": 0}
    )
    project.collection("geometryVersions").document("geometry-a").set(
        {
            "ownerUid": "user-a",
            "status": "needs_review",
            "version": 1,
            "sourceReportId": "report-geometry",
            "analysis": {"facets": [], "warnings": []},
        }
    )
    db.collection("jobs").document("geometry-job").set(
        {
            "ownerUid": "user-a",
            "projectId": "project-geometry",
            "reportId": "report-geometry",
            "type": "geometry_reconstruction",
            "status": "succeeded",
        }
    )
    client = client_for(db, "user-b")
    valid_geometry_edit = {
        "expectedCurrentVersionId": "geometry-a",
        "expectedVersion": 1,
        "facets": [
            {
                "id": "plane-a",
                "name": "Plane A",
                "roofType": "pitched",
                "polygonPagePoints": [[10, 10], [20, 10], [20, 20]],
            }
        ],
    }
    routes = [
        (
            "POST",
            "/api/projects/project-geometry/geometry/reconstruct",
            {"reportId": "report-geometry"},
        ),
        ("GET", "/api/projects/project-geometry/geometry", None),
        ("GET", "/api/projects/project-geometry/geometry/versions", None),
        ("GET", "/api/projects/project-geometry/geometry/page-image?page=1", None),
        ("PUT", "/api/projects/project-geometry/geometry", valid_geometry_edit),
        (
            "POST",
            "/api/projects/project-geometry/geometry/approve",
            {"expectedVersion": 1, "confirmation": True},
        ),
        ("GET", "/api/jobs/geometry-job", None),
    ]
    for method, path, body in routes:
        response = client.request(method, path, json=body)
        assert response.status_code == 404, (method, path, response.text)
    reset_overrides()


def test_geometry_reconstruction_review_approval_and_revision_workflow(monkeypatch) -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-geometry")
    project_ref.set({"ownerUid": "user-a", "name": "Geometry test", "status": "uploaded"})
    monkeypatch.setattr(main, "database", lambda: db)
    client = client_for(db)
    report_path = Path(__file__).parents[3] / "83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf"
    if not report_path.exists():
        import pytest

        pytest.skip("Roofr golden PDF fixture is not available in this checkout")
    uploaded = client.post(
        "/api/projects/project-geometry/reports",
        files={"file": (report_path.name, report_path.read_bytes(), "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert uploaded.status_code == 201, uploaded.text

    queued = client.post(
        "/api/projects/project-geometry/geometry/reconstruct",
        json={"reportId": uploaded.json()["id"]},
    )
    assert queued.status_code == 202, queued.text
    geometry_version_id = queued.json()["geometryVersionId"]
    version = client.get("/api/projects/project-geometry/geometry")
    assert version.status_code == 200, version.text
    assert version.json()["status"] == "needs_review"
    assert version.json()["analysis"]["facetCount"] == 8
    job = client.get(f"/api/jobs/{queued.json()['jobId']}")
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    page_image = client.get(
        f"/api/projects/project-geometry/geometry/page-image?page=4&version_id={geometry_version_id}"
    )
    assert page_image.status_code == 200
    assert page_image.headers["content-type"] == "image/png"
    assert page_image.headers["cache-control"] == "private, no-store"

    blocked = client.post(
        "/api/projects/project-geometry/geometry/approve",
        json={"expectedVersion": 1, "confirmation": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "approval_blocked"

    original_facets = version.json()["analysis"]["facets"]
    edit_facets = [
        {
            "id": facet["id"],
            "name": facet["name"],
            "roofType": facet["roofType"],
            "polygonPagePoints": facet["polygonPagePoints"],
            "pitch": facet.get("pitch"),
            "downslopeDirectionPage": facet.get("downslopeDirectionPage"),
            "edgeTypes": facet.get("edgeTypes", []),
            "reportedAreaSqFt": facet.get("areaLabelSqFt"),
        }
        for facet in original_facets
    ]
    saved = client.put(
        "/api/projects/project-geometry/geometry",
        json={
            "expectedCurrentVersionId": geometry_version_id,
            "expectedVersion": 1,
            "facets": edit_facets,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert saved.json()["basedOnVersionId"] == geometry_version_id
    assert saved.json()["analysis"]["validation"]["checks"]
    stale = client.put(
        "/api/projects/project-geometry/geometry",
        json={
            "expectedCurrentVersionId": geometry_version_id,
            "expectedVersion": 1,
            "facets": edit_facets,
        },
    )
    assert stale.status_code == 409

    # An approved version is immutable. A later review save points the project to a
    # new version while keeping the approved record intact.
    approved_ref = project_ref.collection("geometryVersions").document(saved.json()["id"])
    approved = approved_ref.get().to_dict() or {}
    analysis = approved["analysis"]
    for facet in analysis["facets"]:
        facet["pitch"] = facet.get("pitch") or {"notation": "1:12", "rise": 1, "run": 12}
        facet["downslopeDirectionPage"] = facet.get("downslopeDirectionPage") or [1, 0]
    analysis.setdefault("validation", {})["checks"] = [{"name": "reviewed", "status": "pass"}]
    analysis["validation"]["buildDiagnostics"] = {"unresolvedOverlaps": []}
    approved["analysis"] = analysis
    approved["geometry"] = {
        "schemaVersion": "1.1",
        "planes": [],
        "heightSolve": {"status": "solved"},
    }
    approved_ref.set(approved)
    approval = client.post(
        "/api/projects/project-geometry/geometry/approve",
        json={"expectedVersion": 2, "confirmation": True},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "approved"
    assert approval.json()["approvedByUid"] == "user-a"

    revised = client.put(
        "/api/projects/project-geometry/geometry",
        json={
            "expectedCurrentVersionId": saved.json()["id"],
            "expectedVersion": 2,
            "facets": edit_facets,
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["version"] == 3
    assert revised.json()["basedOnVersionId"] == saved.json()["id"]
    assert (approved_ref.get().to_dict() or {})["status"] == "approved"
    assert (project_ref.get().to_dict() or {})["currentGeometryVersionId"] == revised.json()["id"]
    reset_overrides()


def test_geometry_3d_edit_translates_plan_vertices_to_pdf_facets() -> None:
    from app.schemas import Geometry3DEditRequest

    geometry_version = {
        "ownerUid": "user-a",
        "projectId": "project-3d",
        "geometry": {
            "schemaVersion": "1.2",
            "planes": [
                {
                    "id": "plane-a",
                    "boundaryVertexIds": ["a", "b", "c", "d"],
                    "edgeIds": ["ab", "bc", "cd", "da"],
                }
            ],
            "edges": [
                {"id": "ab", "vertexIds": ["a", "b"], "type": "eave"},
                {"id": "bc", "vertexIds": ["b", "c"], "type": "rake"},
                {"id": "cd", "vertexIds": ["c", "d"], "type": "ridge"},
                {"id": "da", "vertexIds": ["d", "a"], "type": "rake"},
            ],
        },
        "calibration": {"feetPerPoint": 0.5, "originPagePoint": [10, 100]},
    }
    payload = Geometry3DEditRequest.model_validate(
        {
            "expectedCurrentVersionId": "g-current",
            "expectedVersion": 1,
            "vertices": [
                {"id": "a", "plan": [0, 0]},
                {"id": "b", "plan": [10, 0]},
                {"id": "c", "plan": [10, 5]},
                {"id": "d", "plan": [0, 5]},
            ],
            "planes": [
                {
                    "id": "plane-a",
                    "name": "Main roof",
                    "roofType": "pitched",
                    "boundaryVertexIds": ["a", "b", "c", "d"],
                    "pitch": {"notation": "4:12", "rise": 4, "run": 12},
                    "downslopePlanUnitVector": [0, -1],
                    "reportedAreaSqFt": 100,
                    "drawOrder": 0,
                }
            ],
        }
    )

    facets = main._geometry_3d_edit_facets(FakeFirestore(), "g-current", geometry_version, payload)

    assert facets[0].polygonPagePoints == [(10, 100), (30, 100), (30, 90), (10, 90)]
    assert facets[0].downslopeDirectionPage == (0, 1)
    assert facets[0].edgeTypes == ["eave", "rake", "ridge", "rake"]
    assert facets[0].provenance["outline"] == "user_edited"


def test_automatic_geometry_queue_deduplicates_current_review_candidate() -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-auto-geometry")
    project_ref.set(
        {
            "ownerUid": "user-a",
            "currentMeasurementVersionId": "measurement-1",
            "geometryVersionCounter": 0,
        }
    )

    queued = main._queue_geometry_reconstruction(
        db, project_ref, "project-auto-geometry", "user-a", "report-1", force=False
    )
    version_ref = project_ref.collection("geometryVersions").document(queued["geometryVersionId"])
    version_ref.update(
        {
            "status": "needs_review",
            "sourceReportId": "report-1",
            "measurementVersionId": "measurement-1",
        }
    )
    repeated = main._queue_geometry_reconstruction(
        db, project_ref, "project-auto-geometry", "user-a", "report-1", force=False
    )

    assert queued["status"] == "queued"
    assert repeated["status"] == "needs_review"
    assert repeated["geometryVersionId"] == queued["geometryVersionId"]
    assert len(project_ref.collection("geometryVersions").stream()) == 1
    reset_overrides()


def test_geometry_reconstruction_retry_reuses_active_job() -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-active-geometry")
    project_ref.set(
        {
            "ownerUid": "user-a",
            "currentMeasurementVersionId": "measurement-1",
            "geometryVersionCounter": 0,
        }
    )

    first = main._queue_geometry_reconstruction(
        db, project_ref, "project-active-geometry", "user-a", "report-1", force=True
    )
    repeated = main._queue_geometry_reconstruction(
        db, project_ref, "project-active-geometry", "user-a", "report-1", force=True
    )

    assert first["status"] == "queued"
    assert repeated["status"] == "processing"
    assert repeated["jobId"] == first["jobId"]
    assert repeated["geometryVersionId"] == first["geometryVersionId"]
    assert len(project_ref.collection("geometryVersions").stream()) == 1
    reset_overrides()


def test_material_can_be_deleted_from_its_owner_catalog() -> None:
    db = FakeFirestore()
    material = (
        db.collection("users")
        .document("user-a")
        .collection("materialCatalog")
        .document("material-a")
    )
    material.set({"name": "Shingles"})
    response = client_for(db).delete("/api/materials/material-a")
    assert response.status_code == 204
    assert not material.get().exists
    reset_overrides()


def test_job_poll_also_checks_the_referenced_project_owner() -> None:
    db = FakeFirestore()
    db.collection("projects").document("project-a").set({"ownerUid": "user-a"})
    db.collection("jobs").document("job-for-other-project").set(
        {
            "ownerUid": "user-b",
            "projectId": "project-a",
            "type": "extraction",
            "status": "running",
        }
    )
    client = client_for(db, "user-b")
    response = client.get("/api/jobs/job-for-other-project")
    assert response.status_code == 404
    reset_overrides()


def test_material_defaults_follow_runtime_settings() -> None:
    from app.config import Settings

    db = FakeFirestore()
    client = client_for(db)
    main.app.dependency_overrides[main.current_settings] = lambda: Settings(
        _env_file=None,
        default_currency="USD",
        default_waste_percentage="9.5",
    )
    response = client.post(
        "/api/materials",
        json={
            "name": "Test shingles",
            "category": "roofing",
            "unit": "bundle",
            "pricePerUnit": "0",
        },
    )
    assert response.status_code == 201
    assert response.json()["currency"] == "USD"
    assert response.json()["defaultWastePercentage"] == "9.5"
    reset_overrides()


def test_measurement_review_versions_source_waste_and_checks_pointer() -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-a")
    project_ref.set(
        {
            "ownerUid": "user-a",
            "currentMeasurementVersionId": "measurement-old",
            "status": "needs_review",
        }
    )
    old_ref = project_ref.collection("measurementVersions").document("measurement-old")
    old_ref.set(
        {
            "sourceReportId": "report-a",
            "sourceWastePercentage": "8",
            "values": {
                "roofAreaSqFt": {"state": "measured", "value": "7923", "unit": "sq ft"},
                "sourceWastePercentage": {"state": "measured", "value": "8", "unit": "%"},
            },
            "fieldProvenance": {
                "roofAreaSqFt": {
                    "original": {"state": "measured", "value": "7923", "unit": "sq ft"},
                    "corrected": None,
                },
                "sourceWastePercentage": {
                    "original": {"state": "measured", "value": "8", "unit": "%"},
                    "corrected": None,
                },
            },
        }
    )
    client = client_for(db)
    response = client.put(
        "/api/projects/project-a/measurements",
        json={
            "expectedCurrentVersionId": "measurement-old",
            "sourceWastePercentage": "10",
            "values": {
                "roofAreaSqFt": {"state": "measured", "value": "7900", "unit": "sq ft"},
                "sourceWastePercentage": {"state": "measured", "value": "8", "unit": "%"},
            },
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["sourceWastePercentage"] == "10"
    assert saved["values"]["sourceWastePercentage"]["value"] == "10"
    assert saved["fieldProvenance"]["sourceWastePercentage"]["original"]["value"] == "8"
    assert saved["fieldProvenance"]["sourceWastePercentage"]["corrected"]["value"] == "10"
    assert (project_ref.get().to_dict() or {})["currentMeasurementVersionId"] == saved["id"]
    reset_overrides()


def test_roofr_measurement_review_preserves_source_and_updates_flat_system_area() -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-roofr")
    project_ref.set(
        {
            "ownerUid": "user-a",
            "currentMeasurementVersionId": "measurement-roofr",
            "status": "needs_review",
        }
    )
    project_ref.collection("measurementVersions").document("measurement-roofr").set(
        {
            "sourceReportId": "report-roofr",
            "sourceFormat": "roofr",
            "origin": "extracted",
            "sourceWastePercentage": None,
            "predominantPitchNotation": "1:12",
            "totalRoofAreaSqFt": "1158",
            "pitchedRoofAreaSqFt": "643",
            "flatRoofAreaSqFt": "515",
            "roofFacets": 8,
            "values": {
                "roofAreaSqFt": {"state": "measured", "value": "1158", "unit": "sq ft"},
                "pitchedRoofAreaSqFt": {"state": "measured", "value": "643", "unit": "sq ft"},
                "flatRoofAreaSqFt": {"state": "measured", "value": "515", "unit": "sq ft"},
            },
            "sourceMeasurements": {
                "roofAreaSqFt": {"state": "measured", "value": "1158", "unit": "sq ft"},
                "pitchedRoofAreaSqFt": {"state": "measured", "value": "643", "unit": "sq ft"},
                "flatRoofAreaSqFt": {"state": "measured", "value": "515", "unit": "sq ft"},
            },
            "correctedMeasurements": {},
            "fieldProvenance": {},
            "roofSystems": [
                {"roofType": "pitched", "areaSqFt": "643", "selectedSystem": None},
                {"roofType": "flat", "areaSqFt": "515", "selectedSystem": None},
            ],
            "areasByPitch": [{"pitch": "1:12", "areaSqFt": "515", "squares": "5.2"}],
            "geometryStatus": "needs_review",
        }
    )
    response = client_for(db).put(
        "/api/projects/project-roofr/measurements",
        json={
            "expectedCurrentVersionId": "measurement-roofr",
            "values": {
                "flatRoofAreaSqFt": {
                    "state": "measured",
                    "value": "520",
                    "unit": "sq ft",
                    "source": "Contractor review",
                }
            },
        },
    )
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["values"]["roofAreaSqFt"]["value"] == "1158"
    assert reviewed["values"]["pitchedRoofAreaSqFt"]["value"] == "643"
    assert reviewed["values"]["flatRoofAreaSqFt"]["value"] == "520"
    assert reviewed["sourceMeasurements"]["flatRoofAreaSqFt"]["value"] == "515"
    assert reviewed["correctedMeasurements"]["flatRoofAreaSqFt"]["value"] == "520"
    assert reviewed["roofSystems"][1]["areaSqFt"] == "520"
    assert reviewed["areasByPitch"][0]["areaSqFt"] == "515"
    reset_overrides()


def test_report_upload_is_chunked_deduplicated_and_deletable() -> None:
    db = FakeFirestore()
    db.collection("projects").document("project-a").set(
        {"ownerUid": "user-a", "status": "uploaded"}
    )
    client = client_for(db)
    sample_pdf = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    payload = sample_pdf.read_bytes()
    first = client.post(
        "/api/projects/project-a/reports",
        files={"file": ("unsafe path/roof.pdf", payload, "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert first.status_code == 201, first.text
    report_id = first.json()["id"]
    assert first.json()["status"] == "complete"
    report_ref = (
        db.collection("projects").document("project-a").collection("reports").document(report_id)
    )
    report = report_ref.get().to_dict() or {}
    assert report["chunkCount"] > 1
    assert report["originalFilename"] == "roof.pdf"
    second = client.post(
        "/api/projects/project-a/reports",
        files={"file": ("roof.pdf", payload, "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["duplicate"] is True
    download = client.get(f"/api/projects/project-a/reports/{report_id}/file")
    assert download.status_code == 200
    assert download.content == payload
    assert download.headers["cache-control"] == "private, no-store"
    assert client.delete(f"/api/projects/project-a/reports/{report_id}").status_code == 204
    assert report_ref.collection("chunks").stream() == []
    reset_overrides()


def test_roofr_upload_runs_direct_extraction_and_returns_mixed_measurements(monkeypatch) -> None:
    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-roofr")
    project_ref.set(
        {"ownerUid": "user-a", "name": "Roofr test", "address": None, "status": "uploaded"}
    )
    client = client_for(db)
    monkeypatch.setattr(main, "database", lambda: db)
    sample_pdf = Path(__file__).parents[3] / "83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf"
    response = client.post(
        "/api/projects/project-roofr/reports",
        files={"file": (sample_pdf.name, sample_pdf.read_bytes(), "application/pdf")},
        data={"autoExtract": "true"},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["jobId"]
    job = db.collection("jobs").document(job_id).get().to_dict() or {}
    assert job["status"] == "succeeded"
    project = project_ref.get().to_dict() or {}
    assert project["address"] == "83 Rue Falardeau, Gatineau, QC J8X 3E1"
    version = (
        project_ref.collection("measurementVersions")
        .document(project["currentMeasurementVersionId"])
        .get()
        .to_dict()
        or {}
    )
    assert version["sourceFormat"] == "roofr"
    assert version["totalRoofAreaSqFt"] == "1158"
    assert version["pitchedRoofAreaSqFt"] == "643"
    assert version["flatRoofAreaSqFt"] == "515"
    assert version["roofFacets"] == 8
    assert version["geometryStatus"] == "needs_review"
    reset_overrides()


def test_concurrent_duplicate_upload_has_one_report_record() -> None:
    db = FakeFirestore()
    db.collection("projects").document("project-a").set(
        {"ownerUid": "user-a", "status": "uploaded"}
    )
    client = client_for(db)
    sample_pdf = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    payload = sample_pdf.read_bytes()

    def upload_once() -> int:
        response = client.post(
            "/api/projects/project-a/reports",
            files={"file": ("roof.pdf", payload, "application/pdf")},
            data={"autoExtract": "false"},
        )
        assert response.status_code in {201, 409}, response.text
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: upload_once(), range(2)))
    assert 201 in statuses
    reports = db.collection("projects").document("project-a").collection("reports").stream()
    assert len(reports) == 1
    report = reports[0].to_dict() or {}
    assert report["status"] == "complete"
    assert report["chunkCount"] > 0
    reset_overrides()


def test_upload_firestore_failure_cleans_chunks_and_sanitizes_error() -> None:
    from app.config import Settings

    db = FakeFirestore()
    db.collection("projects").document("project-a").set(
        {"ownerUid": "user-a", "status": "uploaded"}
    )
    db.fail_on_batch_number = 2
    client = client_for(db)
    main.app.dependency_overrides[main.current_settings] = lambda: Settings(
        _env_file=None,
        file_chunk_size_bytes=256 * 1024,
        firestore_chunk_batch_size=1,
    )
    sample_pdf = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    response = client.post(
        "/api/projects/project-a/reports",
        files={"file": ("roof.pdf", sample_pdf.read_bytes(), "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert response.status_code == 500
    assert "injected" not in response.text
    report = db.collection("projects").document("project-a").collection("reports").stream()[0]
    assert (report.to_dict() or {})["status"] == "failed"
    assert report.reference.collection("chunks").stream() == []
    reset_overrides()


def test_upload_rejects_size_over_limit_before_storing_report() -> None:
    from app.config import Settings

    db = FakeFirestore()
    db.collection("projects").document("project-a").set({"ownerUid": "user-a"})
    client = client_for(db)
    main.app.dependency_overrides[main.current_settings] = lambda: Settings(
        _env_file=None, max_upload_bytes=100
    )
    sample_pdf = Path(__file__).parents[3] / "RoofManager-Example-Report.pdf"
    response = client.post(
        "/api/projects/project-a/reports",
        files={"file": ("roof.pdf", sample_pdf.read_bytes(), "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert db.collection("projects").document("project-a").collection("reports").stream() == []
    reset_overrides()


def test_project_delete_removes_report_artifact_estimate_chunks_and_jobs() -> None:
    from app.services.file_store import store_file_chunks

    db = FakeFirestore()
    project_ref = db.collection("projects").document("project-a")
    project_ref.set({"ownerUid": "user-a", "status": "uploaded"})
    report_ref = project_ref.collection("reports").document("report-a")
    artifact_ref = project_ref.collection("artifacts").document("artifact-a")
    report_ref.set({"ownerUid": "user-a", "status": "complete"})
    artifact_ref.set({"ownerUid": "user-a", "status": "complete"})
    store_file_chunks(db, report_ref, io.BytesIO(b"report"), chunk_size_bytes=2)
    store_file_chunks(db, artifact_ref, io.BytesIO(b"artifact"), chunk_size_bytes=3)
    estimate_ref = project_ref.collection("estimates").document("estimate-a")
    estimate_ref.set({"ownerUid": "user-a"})
    estimate_ref.collection("lineItems").document("000").set({"description": "Shingles"})
    db.collection("jobs").document("job-a").set({"ownerUid": "user-a", "projectId": "project-a"})
    db.collection("jobs").document("job-b").set({"ownerUid": "user-a", "projectId": "project-b"})

    client = client_for(db)
    response = client.delete("/api/projects/project-a")
    assert response.status_code == 204
    assert project_ref.get().exists is False
    assert report_ref.collection("chunks").stream() == []
    assert artifact_ref.collection("chunks").stream() == []
    assert db.collection("jobs").document("job-a").get().exists is False
    assert db.collection("jobs").document("job-b").get().exists is True
    reset_overrides()


def test_upload_rejects_non_pdf() -> None:
    db = FakeFirestore()
    db.collection("projects").document("project-a").set({"ownerUid": "user-a"})
    client = client_for(db)
    response = client.post(
        "/api/projects/project-a/reports",
        files={"file": ("bad.pdf", b"not pdf", "application/pdf")},
        data={"autoExtract": "false"},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "not_pdf"
    reset_overrides()
