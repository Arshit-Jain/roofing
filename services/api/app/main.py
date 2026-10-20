"""FastAPI routes for auth, projects, reports, measurements, materials, and estimates."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.estimation.engine import calculate_estimate
from app.extraction.pipeline import (
    ExtractionFailure,
    call_nvidia,
    inspect_pdf,
    normalize_report,
    parse_model_json,
    parse_roofr_pdf,
    _safe_geometry_review,
)
from app.firebase import get_current_user, get_database
from app.schemas import (
    EstimateCreate,
    GeometryApproveRequest,
    Geometry3DEditRequest,
    GeometryEditRequest,
    GeometryReconstructRequest,
    MaterialInput,
    MeasurementEdit,
    ProjectCreate,
    ProjectPatch,
)
from app.services.file_store import (
    delete_file_chunks,
    read_file_chunks,
    store_file_chunks,
    verify_chunk_count,
)
from app.services.firestore_values import firestore_safe_value, restore_firestore_value
from app.services.geometry_store import store_geometry_version

logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO))
logger = logging.getLogger("roofing.api")


class UploadBodyLimitMiddleware:
    """Bound multipart request bodies before Starlette's parser writes file parts to its spool."""

    def __init__(self, app: ASGIApp, *, max_upload_bytes: int) -> None:
        self.app = app
        self.request_limit = max_upload_bytes + 128 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/api/projects/")
            or not scope["path"].endswith("/reports")
        ):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > self.request_limit:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "upload_too_large",
                                "message": "PDF exceeds the configured upload limit.",
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "invalid_content_length",
                            "message": "Content-Length header is invalid.",
                        }
                    },
                )
                await response(scope, receive, send)
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.request_limit:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "upload_too_large",
                            "message": "PDF exceeds the configured upload limit.",
                        },
                    )
            return message

        await self.app(scope, bounded_receive, send)


app = FastAPI(title="Roofing Estimator API", version="0.1.0")
app.add_middleware(UploadBodyLimitMiddleware, max_upload_bytes=get_settings().max_upload_bytes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
job_slots = threading.BoundedSemaphore(get_settings().job_max_concurrency)


@app.middleware("http")
async def bounded_request_timeout(request: Request, call_next: Any) -> Any:
    import asyncio

    try:
        return await asyncio.wait_for(
            call_next(request), timeout=current_settings().request_timeout_seconds
        )
    except TimeoutError:
        return JSONResponse(
            status_code=504,
            content={
                "error": {
                    "code": "request_timeout",
                    "message": "The request exceeded the configured time limit.",
                }
            },
        )


@app.exception_handler(Exception)
async def sanitized_unhandled_error(_: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled request failure type=%s", type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={
            "error": {"code": "internal_error", "message": "The request could not be completed."}
        },
    )


@app.exception_handler(HTTPException)
async def sanitized_http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"code": "request_failed", "message": str(exc.detail)}
    )
    return JSONResponse(status_code=exc.status_code, content={"error": detail}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def sanitized_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info("Request validation failed field_count=%s", len(exc.errors()))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "One or more request fields are invalid.",
            }
        },
    )


def current_settings() -> Settings:
    return get_settings()


def database() -> Any:
    try:
        return get_database()
    except Exception as exc:
        logger.error("Firestore initialization failed type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "Firestore is unavailable."},
        ) from exc


def user_id(user: dict[str, Any]) -> str:
    return str(user["uid"])


def _server_time() -> Any:
    from firebase_admin import firestore

    return firestore.SERVER_TIMESTAMP


def owned_project(db: Any, uid: str, project_id: str) -> Any:
    ref = db.collection("projects").document(project_id)
    snapshot = ref.get()
    data = snapshot.to_dict() if snapshot.exists else None
    if not data or data.get("ownerUid") != uid:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    return ref


def clean_filename(value: str | None) -> str:
    source = (value or "report.pdf").replace("\\", "/").split("/")[-1]
    clean = re.sub(r"[^A-Za-z0-9._ -]", "_", source).strip(" .")[:120]
    if not clean.lower().endswith(".pdf"):
        clean = f"{clean or 'report'}.pdf"
    return clean


def _recent(value: Any, seconds: int) -> bool:
    if value is None:
        return False
    if hasattr(value, "to_datetime"):
        value = value.to_datetime()
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return datetime.now(UTC) - cast(datetime, value) < timedelta(seconds=seconds)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _run_transaction(db: Any, callback: Callable[[Any], Any]) -> Any:
    """Run a Firestore callback with the transaction lifecycle managed by the SDK."""
    from firebase_admin import firestore

    run = cast(Callable[[Any], Any], firestore.transactional(callback))
    return run(db.transaction())


def _timestamp_order(data: dict[str, Any]) -> float:
    value = data.get("updatedAt") or data.get("createdAt")
    if value is not None and hasattr(value, "to_datetime"):
        value = value.to_datetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    return 0.0


def _trusted_report_quantity(
    line: Any,
    references: list[dict[str, Any]],
    waste_recommendations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    system_area_reference: dict[str, Any] | None = None
    if line.roofType is not None and line.wastePercentage is not None:
        for recommendation in waste_recommendations or []:
            if (
                recommendation.get("roofType") == line.roofType
                and Decimal(str(recommendation.get("wastePercentage", "-1")))
                == line.wastePercentage
            ):
                try:
                    area = Decimal(str(recommendation["sourceAdjustedAreaSqFt"]))
                except (KeyError, ValueError, TypeError):
                    break
                system_area_reference = {
                    "sourceReportedQuantity": area,
                    "sourceReportedUnit": "sq ft",
                }
                break
    description = line.description.casefold()
    for reference in references:
        material_name = reference.get("material")
        if (
            not isinstance(material_name, str)
            or len(material_name.strip()) < 4
            or material_name.casefold() not in description
        ):
            continue
        if line.roofType is not None and reference.get("roofType") not in {
            None,
            line.roofType,
        }:
            continue
        waste_table = reference.get("quantityByWastePercentage")
        if line.wastePercentage is not None and isinstance(waste_table, dict):
            waste_key = format(line.wastePercentage.normalize(), "f")
            reference_quantity = waste_table.get(waste_key)
            if reference_quantity is not None:
                try:
                    parsed_reference = Decimal(str(reference_quantity))
                except Exception:
                    return {"sourceReportedQuantity": None, "sourceReportedUnit": None}
                if parsed_reference.is_finite() and parsed_reference >= 0:
                    return {
                        "sourceReportedQuantity": parsed_reference,
                        "sourceReportedUnit": str(reference.get("unit", line.unit))[:30],
                    }
        quantity = reference.get("quantity_with_waste")
        unit = reference.get("unit_with_waste")
        if quantity is None:
            quantity = reference.get("quantity")
            unit = reference.get("unit")
        if quantity is None or not isinstance(unit, str):
            return {"sourceReportedQuantity": None, "sourceReportedUnit": None}
        try:
            parsed_quantity = Decimal(str(quantity))
        except Exception:
            return {"sourceReportedQuantity": None, "sourceReportedUnit": None}
        if not parsed_quantity.is_finite() or parsed_quantity < 0:
            return {"sourceReportedQuantity": None, "sourceReportedUnit": None}
        return {"sourceReportedQuantity": parsed_quantity, "sourceReportedUnit": unit[:30]}
    return system_area_reference or {
        "sourceReportedQuantity": None,
        "sourceReportedUnit": None,
    }


def _job_ref(db: Any, job_id: str) -> Any:
    return db.collection("jobs").document(job_id)


def _record_job_progress(
    job_ref: Any,
    events: list[dict[str, str]],
    stage: str,
    message: str,
    *,
    level: str = "info",
) -> None:
    """Persist a short, user-safe event log for a long-running job."""
    occurred_at = datetime.now(UTC).isoformat()
    event = {
        "stage": stage,
        "message": message,
        "level": level,
        "at": occurred_at,
    }
    events.append(event)
    try:
        job_ref.update(
            {
                "progress": {"stage": stage, "message": message, "at": occurred_at},
                "events": events[-30:],
                "updatedAt": _server_time(),
            }
        )
    except Exception as exc:
        # Progress logging must not cause otherwise valid geometry processing to fail.
        logger.warning("Could not persist job progress type=%s", type(exc).__name__)


def extraction_job_id(
    report_sha: str, settings: Settings, project_id: str, *, force: bool = False
) -> str:
    key = ":".join(
        (
            project_id,
            report_sha,
            settings.nvidia_model,
            settings.extraction_schema_version,
            settings.extraction_prompt_version,
        )
    )
    if force:
        key = f"{key}:{uuid.uuid4().hex}"
    return f"j_{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _raw_extraction_artifact_id(report_id: str, settings: Settings) -> str:
    key = ":".join(
        (
            report_id,
            settings.nvidia_model,
            settings.extraction_schema_version,
            settings.extraction_prompt_version,
        )
    )
    return f"raw_{hashlib.sha256(key.encode()).hexdigest()[:24]}"


def _load_cached_raw_extraction(
    project_ref: Any, report_id: str, settings: Settings
) -> tuple[dict[str, Any], str, str] | None:
    """Reuse a complete model response after measurement persistence failures."""
    artifact_id = _raw_extraction_artifact_id(report_id, settings)
    artifact_ref = project_ref.collection("artifacts").document(artifact_id)
    artifact = artifact_ref.get().to_dict() or {}
    if (
        artifact.get("kind") != "raw_extraction_json"
        or artifact.get("sourceReportId") != report_id
        or artifact.get("status") != "complete"
    ):
        return None
    raw_text = artifact.get("rawText")
    if not isinstance(raw_text, str):
        buffer = io.BytesIO()
        try:
            read_file_chunks(artifact_ref, buffer, artifact)
            raw_text = buffer.getvalue().decode("utf-8")
        except Exception as exc:
            logger.warning("Cached extraction artifact could not be read type=%s", type(exc).__name__)
            try:
                artifact_ref.update({"status": "invalid", "updatedAt": _server_time()})
            except Exception:
                pass
            return None
    try:
        raw = parse_model_json(raw_text)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Cached extraction artifact is invalid reportId=%s", report_id)
        try:
            artifact_ref.update({"status": "invalid", "updatedAt": _server_time()})
        except Exception:
            pass
        return None
    return raw, raw_text, artifact_id


def _store_raw_extraction(
    db: Any,
    project_ref: Any,
    report_ref: Any,
    report: dict[str, Any],
    report_id: str,
    raw_text: str,
    settings: Settings,
    *,
    extractor: str = "nvidia_nim",
) -> str:
    """Persist the LLM response before measurement writes, so retries can reuse it."""
    artifact_id = _raw_extraction_artifact_id(report_id, settings)
    artifact_ref = project_ref.collection("artifacts").document(artifact_id)
    existing = artifact_ref.get().to_dict() or {}
    if existing.get("status") != "complete":
        artifact_bytes = raw_text.encode("utf-8")
        artifact_ref.set(
            {
                "kind": "raw_extraction_json",
                "ownerUid": report.get("ownerUid"),
                "sourceReportId": report_id,
                "model": settings.nvidia_model,
                "extractor": extractor,
                "promptVersion": settings.extraction_prompt_version,
                "schemaVersion": settings.extraction_schema_version,
                "contentType": "application/json",
                "status": "uploading",
                "createdAt": _server_time(),
            }
        )
        artifact_metadata = store_file_chunks(
            db,
            artifact_ref,
            io.BytesIO(artifact_bytes),
            chunk_size_bytes=settings.file_chunk_size_bytes,
            batch_size=settings.firestore_chunk_batch_size,
            max_file_size_bytes=settings.max_artifact_bytes,
        )
        artifact_fields = {
            **artifact_metadata.as_document_fields(),
            "status": "complete",
            "updatedAt": _server_time(),
        }
        if len(artifact_bytes) <= settings.geometry_inline_limit_bytes:
            artifact_fields["rawText"] = raw_text
        artifact_ref.update(artifact_fields)
    report_ref.update(
        {
            "rawExtractionArtifactId": artifact_id,
            "rawExtractionModel": settings.nvidia_model,
            "rawExtractionExtractor": extractor,
            "rawExtractionPromptVersion": settings.extraction_prompt_version,
            "rawExtractionSchemaVersion": settings.extraction_schema_version,
            "updatedAt": _server_time(),
        }
    )
    return artifact_id


def _process_extraction_job(job_id: str, project_id: str, report_id: str) -> None:
    with job_slots:
        _execute_extraction_job(job_id, project_id, report_id)
    # Build the project-specific review draft as soon as measurements persist. Geometry
    # runs after releasing the extraction semaphore, since both jobs use the same slots.
    try:
        db = database()
        project_ref = db.collection("projects").document(project_id)
        report_ref = project_ref.collection("reports").document(report_id)
        report = report_ref.get().to_dict() or {}
        project = project_ref.get().to_dict() or {}
        if (
            report.get("extractionStatus") == "succeeded"
            and project.get("currentReportId") == report_id
            and project.get("currentMeasurementVersionId")
        ):
            queued = _queue_geometry_reconstruction(
                db,
                project_ref,
                project_id,
                str(report.get("ownerUid", "")),
                report_id,
                force=False,
            )
            if queued["status"] == "queued":
                _run_geometry_reconstruction(
                    queued["jobId"], project_id, report_id, queued["geometryVersionId"]
                )
    except Exception as exc:
        logger.warning("Automatic geometry reconstruction could not start type=%s", type(exc).__name__)


def _execute_extraction_job(job_id: str, project_id: str, report_id: str) -> None:
    """Persistent job execution requires one live API instance."""
    db = database()
    settings = current_settings()
    job_ref = _job_ref(db, job_id)
    now = _server_time()
    previous_attempt = int((job_ref.get().to_dict() or {}).get("attempt", 0))
    job_ref.update(
        {"status": "running", "startedAt": now, "updatedAt": now, "attempt": previous_attempt + 1}
    )
    project_ref = db.collection("projects").document(project_id)
    report_ref = project_ref.collection("reports").document(report_id)
    report_ref.update({"extractionStatus": "processing", "updatedAt": _server_time()})
    scratch = tempfile.mkdtemp(prefix="roofing-extract-")
    os.chmod(scratch, 0o700)
    pdf_path = Path(scratch) / "source.pdf"
    stage = "reading_pdf"
    operation = "read_report_chunks"
    try:
        report = report_ref.get().to_dict() or {}
        with pdf_path.open("wb") as output:
            read_file_chunks(report_ref, output, report)
        operation = "inspect_pdf"
        stage = "validating_pdf"
        inspect_pdf(pdf_path, settings.max_pdf_pages)
        cached = _load_cached_raw_extraction(project_ref, report_id, settings)
        if cached is None:
            direct_roofr = parse_roofr_pdf(pdf_path)
            if direct_roofr is not None:
                raw, raw_text = direct_roofr
                extractor = "roofr_pdf_parser"
            else:
                operation = "call_nvidia"
                stage = "calling_nvidia"
                raw, raw_text = call_nvidia(pdf_path, settings)
                extractor = "nvidia_nim"
            operation = "store_extraction_artifact_chunks"
            stage = "saving_extraction_artifact"
            artifact_id = _store_raw_extraction(
                db,
                project_ref,
                report_ref,
                report,
                report_id,
                raw_text,
                settings,
                extractor=extractor,
            )
        else:
            raw, raw_text, artifact_id = cached
            operation = "normalize_report"
            stage = "normalizing_measurements"
        operation = "normalize_report"
        stage = "normalizing_measurements"
        normalized = normalize_report(
            raw, report_id=report_id, schema_version=settings.extraction_schema_version
        )
        version_id = f"m_{uuid.uuid4().hex}"
        normalized_data = normalized.model_dump(mode="json")
        normalized_data.update(
            {
                "rawExtractionArtifactId": artifact_id,
                "rawExtractionSha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                "createdAt": _server_time(),
            }
        )
        version_ref = project_ref.collection("measurementVersions").document(version_id)
        operation = "begin_measurement_transaction"
        stage = "saving_measurements"
        from firebase_admin import firestore

        transaction = db.transaction(max_attempts=settings.firebase_retry_attempts + 1)

        def persist_measurements_callback(tx: Any) -> None:
            nonlocal operation
            operation = "read_project_in_transaction"
            snapshot = project_ref.get(transaction=tx)
            operation = "read_report_in_transaction"
            report_snapshot = report_ref.get(transaction=tx)
            operation = "read_job_in_transaction"
            job_snapshot = job_ref.get(transaction=tx)
            project_data = snapshot.to_dict() or {}
            report_data = report_snapshot.to_dict() or {}
            job_data = job_snapshot.to_dict() or {}
            if (
                not snapshot.exists
                or project_data.get("ownerUid") != report.get("ownerUid")
                or project_data.get("status") == "deleting"
            ):
                raise RuntimeError("Project is no longer available for extraction")
            if report_data.get("status") != "complete" or job_data.get("status") != "running":
                raise RuntimeError("Extraction was cancelled because its source report changed")
            tx.set(version_ref, normalized_data)
            operation = "update_project_in_transaction"
            tx.update(
                project_ref,
                {
                    "currentMeasurementVersionId": version_id,
                    "status": "needs_review",
                    "updatedAt": _server_time(),
                    **(
                        {"address": raw.get("property", {}).get("address")}
                        if isinstance(raw.get("property"), dict)
                        and isinstance(raw.get("property", {}).get("address"), str)
                        and not project_data.get("address")
                        else {}
                    ),
                },
            )
            operation = "update_job_in_transaction"
            tx.update(
                job_ref,
                {
                    "status": "succeeded",
                    "resultRefs": {
                        "measurementVersionId": version_id,
                        "rawExtractionArtifactId": artifact_id,
                    },
                    "finishedAt": _server_time(),
                    "updatedAt": _server_time(),
                },
            )
            operation = "commit_measurement_transaction"

        persist_measurements = cast(
            Callable[[Any], None], firestore.transactional(persist_measurements_callback)
        )
        persist_measurements(transaction)
        if (report_ref.get().to_dict() or {}).get("status") == "complete":
            report_ref.update(
                {
                    "extractionStatus": "succeeded",
                    "extractionError": None,
                    "extractionErrorCode": None,
                    "extractionErrorStage": None,
                    "extractionErrorOperation": None,
                    "updatedAt": _server_time(),
                }
            )
    except Exception as exc:
        logger.warning(
            "Extraction job failed jobId=%s stage=%s operation=%s type=%s location=%s:%s:%s",
            job_id,
            stage,
            operation,
            type(exc).__name__,
            Path(traceback.extract_tb(exc.__traceback__)[-1].filename).name
            if exc.__traceback__
            else "unknown",
            traceback.extract_tb(exc.__traceback__)[-1].lineno if exc.__traceback__ else 0,
            traceback.extract_tb(exc.__traceback__)[-1].name if exc.__traceback__ else "unknown",
        )
        if isinstance(exc, ExtractionFailure):
            error_code = exc.code
            safe_message = exc.safe_message
        elif stage == "validating_pdf" and isinstance(exc, ValueError):
            error_code = "invalid_pdf"
            safe_message = str(exc)[:240]
        elif stage == "normalizing_measurements":
            error_code = "invalid_measurement_response"
            safe_message = (
                "The extracted response could not be normalized into roofing measurements. "
                "No results were saved; retry extraction."
            )
        elif stage == "saving_extraction_artifact":
            error_code = "extraction_artifact_storage_failed"
            safe_message = (
                "The model returned a response, but the response could not be saved for recovery. "
                "Retry extraction; the model may need to run again."
            )
        elif stage == "saving_measurements":
            error_code = "measurement_storage_failed"
            safe_message = (
                "The extracted response is saved. Retry extraction to retry the measurement write; "
                "the saved response will be reused."
            )
        elif stage == "reading_source_measurements":
            error_code = "source_measurements_unreadable"
            safe_message = (
                "The source PDF could not be read as a supported roofing measurement report. "
                "Retry with a valid Roofr or Roof Manager PDF, or contact support."
            )
        elif stage == "reading_pdf":
            error_code = "stored_report_unavailable"
            safe_message = (
                "The stored report could not be verified. Re-upload the PDF or contact support."
            )
        else:
            error_code = "extraction_storage_failed"
            safe_message = "Measurements could not be saved. Retry extraction or contact support."
        if report_ref.get().exists:
            report_ref.update(
                {
                    "extractionStatus": "failed",
                    "extractionError": safe_message[:240],
                    "extractionErrorCode": error_code,
                    "extractionErrorStage": stage,
                    "extractionErrorOperation": operation,
                    "updatedAt": _server_time(),
                }
            )
        if job_ref.get().exists:
            job_ref.update(
                {
                    "status": "failed",
                    "error": {
                        "code": error_code,
                        "message": safe_message[:240],
                        "stage": stage,
                        "operation": operation,
                    },
                    "finishedAt": _server_time(),
                    "updatedAt": _server_time(),
                }
            )
        project_data = project_ref.get().to_dict() or {}
        if project_data and project_data.get("status") != "deleting":
            project_ref.update({"status": "failed", "updatedAt": _server_time()})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@app.on_event("startup")
def mark_stale_jobs_failed() -> None:
    """Mark interrupted running jobs for operator retry on the next startup."""
    try:
        db = get_database()
        settings = current_settings()
        for snapshot in db.collection("jobs").where("status", "==", "running").stream():
            data = snapshot.to_dict() or {}
            started = data.get("startedAt")
            if started and not _recent(started, settings.job_stale_after_seconds):
                snapshot.reference.update(
                    {
                        "status": "failed",
                        "error": {
                            "code": "worker_interrupted",
                            "message": "Processing stopped before completion; retry the job.",
                        },
                        "finishedAt": _server_time(),
                        "updatedAt": _server_time(),
                    }
                )
                project_id = data.get("projectId")
                report_id = data.get("reportId")
                if project_id and report_id:
                    project_ref = db.collection("projects").document(project_id)
                    report_ref = project_ref.collection("reports").document(report_id)
                    report = report_ref.get().to_dict() or {}
                    if report:
                        report_ref.update(
                            {
                                "extractionStatus": "failed",
                                "extractionError": (
                                    "Processing stopped before completion; "
                                    "retry the extraction job."
                                ),
                                "updatedAt": _server_time(),
                            }
                        )
                        project_ref.update({"status": "failed", "updatedAt": _server_time()})
    except Exception as exc:
        logger.warning("Stale job sweep skipped type=%s", type(exc).__name__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Any = Depends(database)) -> dict[str, str]:
    try:
        db.collection("_health").limit(1).get()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_ready", "message": "Firestore readiness check failed."},
        ) from exc
    return {"status": "ready"}


@app.get("/api/me")
def me(
    user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> dict[str, Any]:
    return {
        "uid": user_id(user),
        "displayName": user.get("name", ""),
        "email": user.get("email", ""),
    }


@app.get("/api/config")
def public_app_config(
    user: dict[str, Any] = Depends(get_current_user), settings: Settings = Depends(current_settings)
) -> dict[str, Any]:
    del user
    return {
        "currency": settings.default_currency,
        "defaultTaxRate": str(settings.default_tax_rate),
        "defaultWastePercentage": str(settings.default_waste_percentage),
        "defaultMarkupPercentage": str(settings.default_markup_percentage),
        "maxUploadBytes": settings.max_upload_bytes,
        "maxPdfPages": settings.max_pdf_pages,
        "jobPollIntervalSeconds": settings.job_poll_interval_seconds,
        "jobPollTimeoutSeconds": settings.job_poll_timeout_seconds,
    }


@app.post("/api/projects", status_code=201)
def create_project(
    payload: ProjectCreate,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    ref = db.collection("projects").document()
    data = {
        "ownerUid": user_id(user),
        "name": payload.name.strip(),
        "address": payload.address,
        "customer": payload.customer.model_dump() if payload.customer else None,
        "status": "uploaded",
        "currentReportId": None,
        "currentMeasurementVersionId": None,
        "currentGeometryVersionId": None,
        "materialSelections": {},
        "createdAt": _server_time(),
        "updatedAt": _server_time(),
    }
    ref.create(data)
    return {"id": ref.id, **(ref.get().to_dict() or {})}


@app.get("/api/projects")
def list_projects(
    user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> list[dict[str, Any]]:
    rows = list(db.collection("projects").where("ownerUid", "==", user_id(user)).stream())
    rows.sort(key=lambda row: _timestamp_order(row.to_dict() or {}), reverse=True)
    return [{"id": row.id, **(row.to_dict() or {})} for row in rows]


@app.get("/api/projects/{project_id}")
def get_project(
    project_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> dict[str, Any]:
    ref = owned_project(db, user_id(user), project_id)
    return {"id": project_id, **(ref.get().to_dict() or {})}


@app.patch("/api/projects/{project_id}")
def patch_project(
    project_id: str,
    payload: ProjectPatch,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    ref = owned_project(db, user_id(user), project_id)
    changes = payload.model_dump(exclude_unset=True)
    if "customer" in changes and payload.customer:
        changes["customer"] = payload.customer.model_dump()
    if (
        "materialSelections" in changes
        and len(changes["materialSelections"] or {}) > current_settings().max_material_selections
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_material_selections",
                "message": "Too many material selections.",
            },
        )
    changes["updatedAt"] = _server_time()
    ref.update(changes)
    return {"id": project_id, **(ref.get().to_dict() or {})}


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> None:
    project_ref = owned_project(db, user_id(user), project_id)
    project_ref.update({"status": "deleting", "updatedAt": _server_time()})
    for collection_name in (
        "reports",
        "artifacts",
        "measurementVersions",
        "geometryVersions",
        "estimates",
    ):
        for item in project_ref.collection(collection_name).stream():
            if collection_name in {"reports", "artifacts"}:
                item.reference.update({"status": "deleting", "updatedAt": _server_time()})
                delete_file_chunks(
                    db,
                    item.reference,
                    batch_size=settings.firestore_delete_batch_size,
                    retry_attempts=settings.firebase_retry_attempts,
                )
            if collection_name == "estimates":
                for line in item.reference.collection("lineItems").stream():
                    line.reference.delete()
            item.reference.delete()
    for job in db.collection("jobs").where("projectId", "==", project_id).stream():
        job_data = job.to_dict() or {}
        if job_data.get("ownerUid") == user_id(user) and job_data.get("projectId") == project_id:
            job.reference.delete()
    project_ref.delete()


@app.post("/api/projects/{project_id}/reports", status_code=201)
async def upload_report(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    project_ref = await run_in_threadpool(owned_project, db, user_id(user), project_id)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_content_length",
                    "message": "Content-Length header is invalid.",
                },
            ) from exc
        if declared_length > settings.max_upload_bytes + 128 * 1024:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "upload_too_large",
                    "message": "PDF exceeds the configured upload limit.",
                },
            )
    form = await request.form(max_files=1, max_fields=2)
    uploaded = form.get("file")
    if not isinstance(uploaded, UploadFile):
        await form.close()
        raise HTTPException(
            status_code=422, detail={"code": "file_required", "message": "Select a PDF file."}
        )
    auto_extract_raw = form.get("autoExtract", "true")
    if str(auto_extract_raw).lower() not in {"true", "false", "1", "0"}:
        await form.close()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_auto_extract",
                "message": "autoExtract must be true or false.",
            },
        )
    auto_extract = str(auto_extract_raw).lower() in {"true", "1"}
    scratch = tempfile.mkdtemp(prefix="roofing-upload-")
    os.chmod(scratch, 0o700)
    path = Path(scratch) / "upload.pdf"
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as target:
            while True:
                block = await uploaded.read(settings.upload_read_size_bytes)
                if not block:
                    break
                size += len(block)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "upload_too_large",
                            "message": "PDF exceeds the configured upload limit.",
                        },
                    )
                if size == len(block) and not block.startswith(b"%PDF-"):
                    raise HTTPException(
                        status_code=415,
                        detail={"code": "not_pdf", "message": "The uploaded file is not a PDF."},
                    )
                digest.update(block)
                target.write(block)
        if size == 0:
            raise HTTPException(
                status_code=422,
                detail={"code": "empty_file", "message": "The selected PDF is empty."},
            )
        try:
            await run_in_threadpool(inspect_pdf, path, settings.max_pdf_pages)
        except ValueError as exc:
            raise HTTPException(
                status_code=415, detail={"code": "invalid_pdf", "message": str(exc)}
            ) from exc
        sha = digest.hexdigest()
        report_id = f"r_{sha[:32]}"
        report_ref = project_ref.collection("reports").document(report_id)
        created = {
            "ownerUid": user_id(user),
            "originalFilename": clean_filename(uploaded.filename),
            "contentType": "application/pdf",
            "fileSizeBytes": size,
            "sha256": sha,
            "chunkSizeBytes": settings.file_chunk_size_bytes,
            "chunkCount": 0,
            "status": "uploading",
            "extractionStatus": "queued" if auto_extract else "not_started",
            "createdAt": _server_time(),
            "updatedAt": _server_time(),
        }
        try:
            await run_in_threadpool(report_ref.create, created)
        except Exception as exc:
            existing_snapshot = await run_in_threadpool(report_ref.get)
            existing = existing_snapshot.to_dict()
            if not existing:
                raise
            if existing.get("ownerUid") != user_id(user):
                raise HTTPException(
                    status_code=404, detail={"code": "not_found", "message": "Report not found."}
                ) from exc
            if existing.get("status") == "complete":
                return {"id": report_id, "status": "complete", "duplicate": True, "jobId": None}
            if existing.get("status") == "uploading" and _recent(
                existing.get("updatedAt"), settings.upload_stale_after_seconds
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "upload_in_progress",
                        "message": "This PDF is already being uploaded.",
                    },
                ) from exc
            await run_in_threadpool(
                delete_file_chunks,
                db,
                report_ref,
                batch_size=settings.firestore_delete_batch_size,
                retry_attempts=settings.firebase_retry_attempts,
            )
            await run_in_threadpool(report_ref.set, created)
        try:
            with path.open("rb") as source:
                metadata = await run_in_threadpool(
                    store_file_chunks,
                    db,
                    report_ref,
                    source,
                    chunk_size_bytes=settings.file_chunk_size_bytes,
                    batch_size=settings.firestore_chunk_batch_size,
                    max_file_size_bytes=settings.max_upload_bytes,
                    retry_attempts=settings.firebase_retry_attempts,
                )
            await run_in_threadpool(verify_chunk_count, report_ref, metadata.chunk_count)
            if metadata.file_size_bytes != size or metadata.sha256 != sha:
                raise RuntimeError("Uploaded file integrity changed during storage")
            await run_in_threadpool(
                report_ref.update,
                {
                    **metadata.as_document_fields(),
                    "status": "complete",
                    "completedAt": _server_time(),
                    "updatedAt": _server_time(),
                },
            )
            await run_in_threadpool(
                project_ref.update,
                {
                    "currentReportId": report_id,
                    "status": "processing" if auto_extract else "uploaded",
                    "updatedAt": _server_time(),
                },
            )
        except Exception:
            await run_in_threadpool(
                delete_file_chunks,
                db,
                report_ref,
                batch_size=settings.firestore_delete_batch_size,
                retry_attempts=settings.firebase_retry_attempts,
            )
            await run_in_threadpool(
                report_ref.update,
                {
                    "status": "failed",
                    "extractionStatus": "not_started",
                    "updatedAt": _server_time(),
                },
            )
            raise
        job_id = None
        if auto_extract:
            job_id = extraction_job_id(sha, settings, project_id)
            job_ref = _job_ref(db, job_id)
            try:
                await run_in_threadpool(
                    job_ref.create,
                    {
                        "ownerUid": user_id(user),
                        "projectId": project_id,
                        "reportId": report_id,
                        "type": "extraction",
                        "status": "queued",
                        "attempt": 0,
                        "createdAt": _server_time(),
                        "updatedAt": _server_time(),
                    },
                )
                background_tasks.add_task(_process_extraction_job, job_id, project_id, report_id)
            except Exception:
                existing_job = await run_in_threadpool(job_ref.get)
                if existing_job.exists and (existing_job.to_dict() or {}).get("status") == "failed":
                    await run_in_threadpool(
                        job_ref.update,
                        {"status": "queued", "error": None, "updatedAt": _server_time()},
                    )
                    background_tasks.add_task(
                        _process_extraction_job, job_id, project_id, report_id
                    )
        return {"id": report_id, "status": "complete", "duplicate": False, "jobId": job_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Report upload failed type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "upload_failed", "message": "The PDF could not be stored."},
        ) from exc
    finally:
        await uploaded.close()
        await form.close()
        shutil.rmtree(scratch, ignore_errors=True)


@app.get("/api/projects/{project_id}/reports")
def list_reports(
    project_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> list[dict[str, Any]]:
    project_ref = owned_project(db, user_id(user), project_id)
    rows = list(project_ref.collection("reports").stream())
    rows.sort(key=lambda row: _timestamp_order(row.to_dict() or {}), reverse=True)
    return [{"id": row.id, **(row.to_dict() or {})} for row in rows]


@app.get("/api/projects/{project_id}/reports/{report_id}/file")
def download_report(
    project_id: str,
    report_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> FileResponse:
    project_ref = owned_project(db, user_id(user), project_id)
    report_ref = project_ref.collection("reports").document(report_id)
    snapshot = report_ref.get()
    report = snapshot.to_dict() if snapshot.exists else None
    if not report or report.get("ownerUid") != user_id(user) or report.get("status") != "complete":
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Report not found."}
        )
    scratch = tempfile.mkdtemp(prefix="roofing-download-")
    os.chmod(scratch, 0o700)
    path = Path(scratch) / "report.pdf"
    try:
        with path.open("wb") as output:
            read_file_chunks(report_ref, output, report)
    except Exception as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        logger.error(
            "Report integrity check failed projectId=%s reportId=%s", project_id, report_id
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "file_integrity_error",
                "message": "The stored PDF failed its integrity check.",
            },
        ) from exc
    filename = clean_filename(str(report.get("originalFilename", "report.pdf")))
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(shutil.rmtree, scratch, True),
    )


@app.delete("/api/projects/{project_id}/reports/{report_id}", status_code=204)
def delete_report(
    project_id: str,
    report_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> None:
    project_ref = owned_project(db, user_id(user), project_id)
    report_ref = project_ref.collection("reports").document(report_id)
    snapshot = report_ref.get()
    report = snapshot.to_dict() if snapshot.exists else None
    if not report or report.get("ownerUid") != user_id(user):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Report not found."}
        )
    report_ref.update({"status": "deleting", "updatedAt": _server_time()})
    delete_file_chunks(
        db,
        report_ref,
        batch_size=settings.firestore_delete_batch_size,
        retry_attempts=settings.firebase_retry_attempts,
    )
    report_ref.delete()
    for job in db.collection("jobs").where("projectId", "==", project_id).stream():
        job_data = job.to_dict() or {}
        if job_data.get("reportId") == report_id and job_data.get("ownerUid") == user_id(user):
            job.reference.update(
                {
                    "status": "failed",
                    "error": {
                        "code": "report_deleted",
                        "message": "The source report was deleted.",
                    },
                    "finishedAt": _server_time(),
                    "updatedAt": _server_time(),
                }
            )
    project_data = project_ref.get().to_dict() or {}
    if project_data.get("currentReportId") == report_id:
        next_status = (
            "needs_review" if project_data.get("currentMeasurementVersionId") else "uploaded"
        )
        project_ref.update(
            {"currentReportId": None, "status": next_status, "updatedAt": _server_time()}
        )


@app.post("/api/projects/{project_id}/reports/{report_id}/extract", status_code=202)
def retry_extraction(
    project_id: str,
    report_id: str,
    background_tasks: BackgroundTasks,
    force: bool = False,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, str]:
    project_ref = owned_project(db, user_id(user), project_id)
    report_ref = project_ref.collection("reports").document(report_id)
    report = report_ref.get().to_dict()
    if not report or report.get("ownerUid") != user_id(user) or report.get("status") != "complete":
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Report not found."}
        )
    job_id = extraction_job_id(str(report["sha256"]), settings, project_id, force=force)
    ref = _job_ref(db, job_id)
    existing = ref.get().to_dict()
    if existing and existing.get("status") in {"queued", "running", "succeeded"}:
        return {"jobId": job_id, "status": str(existing["status"])}
    if existing:
        ref.update(
            {
                "status": "queued",
                "attempt": int(existing.get("attempt", 0)),
                "error": None,
                "updatedAt": _server_time(),
            }
        )
    else:
        ref.create(
            {
                "ownerUid": user_id(user),
                "projectId": project_id,
                "reportId": report_id,
                "type": "extraction",
                "status": "queued",
                "attempt": 0,
                "createdAt": _server_time(),
                "updatedAt": _server_time(),
            }
        )
    project_ref.update({"status": "processing", "updatedAt": _server_time()})
    background_tasks.add_task(_process_extraction_job, job_id, project_id, report_id)
    return {"jobId": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(
    job_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> dict[str, Any]:
    snapshot = _job_ref(db, job_id).get()
    job = snapshot.to_dict() if snapshot.exists else None
    if not job or job.get("ownerUid") != user_id(user):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Job not found."}
        )
    project_id = job.get("projectId")
    if not isinstance(project_id, str):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Job not found."}
        )
    owned_project(db, user_id(user), project_id)
    return {"id": job_id, **job}


@app.get("/api/projects/{project_id}/measurements")
def current_measurements(
    project_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> dict[str, Any]:
    project = owned_project(db, user_id(user), project_id).get().to_dict() or {}
    version_id = project.get("currentMeasurementVersionId")
    if not version_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "measurements_missing",
                "message": "No measurement version is available yet.",
            },
        )
    version_ref = (
        db.collection("projects")
        .document(project_id)
        .collection("measurementVersions")
        .document(version_id)
    )
    version = version_ref.get().to_dict()
    if not version:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "measurements_missing",
                "message": "No measurement version is available yet.",
            },
        )
    return {"id": version_id, **version}


@app.put("/api/projects/{project_id}/measurements")
def save_measurements(
    project_id: str,
    payload: MeasurementEdit,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    current_id = project.get("currentMeasurementVersionId")
    if payload.expectedCurrentVersionId and payload.expectedCurrentVersionId != current_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "Measurements changed in another session. Reload before saving.",
            },
        )
    if not current_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "measurements_missing",
                "message": "No measurement version is available yet.",
            },
        )
    old_ref = project_ref.collection("measurementVersions").document(current_id)
    old = old_ref.get().to_dict() or {}
    provenance = old.get("fieldProvenance", {})
    new_provenance = json.loads(json.dumps(provenance, default=str))
    original_values = old.get("values", {})
    reviewed_values = json.loads(json.dumps(original_values, default=str))
    reviewed_values.update(
        {key: value.model_dump(mode="json") for key, value in payload.values.items()}
    )
    source_measurements = old.get("sourceMeasurements", old.get("values", {}))
    corrected_measurements = json.loads(
        json.dumps(old.get("correctedMeasurements", {}), default=str)
    )
    for key, value in payload.values.items():
        original = (provenance.get(key) or {}).get("original", source_measurements.get(key))
        corrected = value.model_dump(mode="json")
        changed = corrected != original
        new_provenance[key] = {"original": original, "corrected": corrected if changed else None}
        if changed:
            corrected_measurements[key] = corrected
        else:
            corrected_measurements.pop(key, None)
    reviewed_waste = payload.sourceWastePercentage
    if reviewed_waste is not None:
        old_waste = (old.get("values") or {}).get("sourceWastePercentage")
        original_waste = (provenance.get("sourceWastePercentage") or {}).get("original", old_waste)
        corrected_waste = {
            "state": "zero" if reviewed_waste == 0 else "measured",
            "value": str(reviewed_waste),
            "unit": "%",
            "source": "Contractor review",
        }
        reviewed_values["sourceWastePercentage"] = corrected_waste
        new_provenance["sourceWastePercentage"] = {
            "original": original_waste,
            "corrected": corrected_waste,
        }
        corrected_measurements["sourceWastePercentage"] = corrected_waste
    corrected_pitch = payload.predominantPitchNotation
    if corrected_pitch is not None and corrected_pitch != old.get("predominantPitchNotation"):
        new_provenance["predominantPitchNotation"] = {
            "original": old.get("predominantPitchNotation"),
            "corrected": corrected_pitch,
        }

    def edited_area(name: str, previous: Any) -> Any:
        item = payload.values.get(name)
        if item is None:
            return previous
        return item.value if item.state in {"measured", "zero"} else None

    edited_roof_area = edited_area("roofAreaSqFt", old.get("totalRoofAreaSqFt"))
    edited_pitched_area = edited_area("pitchedRoofAreaSqFt", old.get("pitchedRoofAreaSqFt"))
    edited_flat_area = edited_area("flatRoofAreaSqFt", old.get("flatRoofAreaSqFt"))
    reviewed_systems = json.loads(json.dumps(old.get("roofSystems", []), default=str))
    for system in reviewed_systems:
        if system.get("roofType") == "pitched":
            system["areaSqFt"] = (
                str(edited_pitched_area) if edited_pitched_area is not None else None
            )
        elif system.get("roofType") == "flat":
            system["areaSqFt"] = str(edited_flat_area) if edited_flat_area is not None else None
    facet_quantity = payload.values.get("roofPlaneCount")
    if facet_quantity is None:
        reviewed_facet_count = old.get("roofFacets")
    elif facet_quantity.state in {"measured", "zero"} and facet_quantity.value is not None:
        reviewed_facet_count = int(facet_quantity.value)
    else:
        reviewed_facet_count = None
    version_id = f"m_{uuid.uuid4().hex}"
    data = {
        "sourceReportId": old.get("sourceReportId"),
        "schemaVersion": settings.extraction_schema_version,
        "origin": "user_reviewed",
        "basedOnVersionId": current_id,
        "sourceWastePercentage": reviewed_waste
        if reviewed_waste is not None
        else old.get("sourceWastePercentage"),
        "predominantPitchNotation": payload.predominantPitchNotation
        if payload.predominantPitchNotation is not None
        else old.get("predominantPitchNotation"),
        "materialQuantityReferences": old.get("materialQuantityReferences", []),
        "values": reviewed_values,
        "fieldProvenance": new_provenance,
        "warnings": old.get("warnings", []),
        "sourceFormat": old.get("sourceFormat", "roof_manager"),
        "totalRoofAreaSqFt": edited_roof_area,
        "pitchedRoofAreaSqFt": edited_pitched_area,
        "flatRoofAreaSqFt": edited_flat_area,
        "roofFacets": reviewed_facet_count,
        "areasByPitch": old.get("areasByPitch", []),
        "roofPlanes": old.get("roofPlanes", []),
        "roofSystems": reviewed_systems,
        "edgeLengths": old.get("edgeLengths", {}),
        "flashings": old.get("flashings", {}),
        "transitions": old.get("transitions", {}),
        "wasteRecommendations": old.get("wasteRecommendations", []),
        "sourceMeasurements": source_measurements,
        "correctedMeasurements": corrected_measurements,
        "geometryStatus": old.get("geometryStatus", "not_started"),
        "geometryEvidence": old.get("geometryEvidence", {}),
        "createdAt": _server_time(),
    }
    version_ref = project_ref.collection("measurementVersions").document(version_id)

    def persist_reviewed_version(transaction: Any) -> None:
        snapshot = project_ref.get(transaction=transaction)
        if (snapshot.to_dict() or {}).get("currentMeasurementVersionId") != current_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_conflict",
                    "message": "Measurements changed in another session. Reload before saving.",
                },
            )
        transaction.set(version_ref, _jsonable(data))
        transaction.update(
            project_ref,
            {
                "currentMeasurementVersionId": version_id,
                "status": "ready",
                "updatedAt": _server_time(),
            },
        )

    _run_transaction(db, persist_reviewed_version)
    return {"id": version_id, **_jsonable(version_ref.get().to_dict() or {})}


def _geometry_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _read_geometry_report(project_ref: Any, report_id: str, uid: str) -> tuple[Any, dict[str, Any]]:
    report_ref = project_ref.collection("reports").document(report_id)
    snapshot = report_ref.get()
    report = snapshot.to_dict() if snapshot.exists else None
    if not report or report.get("ownerUid") != uid or report.get("status") != "complete":
        raise _geometry_error("not_found", "Measurement report not found.", 404)
    return report_ref, report


def _apply_llm_geometry_evidence(
    analysis: dict[str, Any],
    measurement: dict[str, Any],
    settings: Settings,
    report_name: str,
) -> bool:
    """Use NVIDIA's diagram points as review candidates in the geometry workflow."""
    evidence = measurement.get("geometryEvidence")
    if not isinstance(evidence, dict):
        return False
    raw_facets = evidence.get("facets")
    if not isinstance(raw_facets, list) or not raw_facets:
        return False

    raw_page = evidence.get("coordinatePageNumber")
    if not isinstance(raw_page, int) or isinstance(raw_page, bool) or raw_page < 1:
        analysis.setdefault("warnings", []).append(
            "NVIDIA returned facet points without a valid source page number; "
            "vector outlines remain active."
        )
        return False
    analysis_page = analysis.get("coordinatePageNumber")
    if analysis_page is not None and analysis_page != raw_page:
        analysis.setdefault("warnings", []).append(
            "NVIDIA's facet points use a different PDF page than the calibrated vector analysis; "
            "the points were not mixed across coordinate frames."
        )
        return False

    page_size = evidence.get("coordinatePageSize")
    if not isinstance(page_size, dict):
        analysis.setdefault("warnings", []).append(
            "NVIDIA returned facet points without page dimensions; vector outlines remain active."
        )
        return False

    def finite_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    width = finite_number(page_size.get("widthPoints"))
    height = finite_number(page_size.get("heightPoints"))
    if width is None or height is None or width <= 0 or height <= 0:
        analysis.setdefault("warnings", []).append(
            "NVIDIA returned invalid page dimensions; vector outlines remain active."
        )
        return False
    actual_page_size = analysis.get("coordinatePageSize")
    if isinstance(actual_page_size, dict):
        actual_width = finite_number(actual_page_size.get("widthPoints"))
        actual_height = finite_number(actual_page_size.get("heightPoints"))
        if (
            actual_width is not None
            and actual_height is not None
            and (abs(actual_width - width) > 1.0 or abs(actual_height - height) > 1.0)
        ):
            analysis.setdefault("warnings", []).append(
                "NVIDIA's page size does not match the calibrated PDF page; "
                "the points were not mixed across coordinate frames."
            )
            return False

    allowed_edge_types = {
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
    facets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_facet in enumerate(raw_facets, start=1):
        if not isinstance(raw_facet, dict):
            continue
        raw_points = raw_facet.get("polygonPagePoints")
        if not isinstance(raw_points, list) or not 3 <= len(raw_points) <= 100:
            continue
        points: list[list[float]] = []
        for raw_point in raw_points:
            if isinstance(raw_point, dict):
                x = finite_number(raw_point.get("x"))
                y = finite_number(raw_point.get("y"))
            elif isinstance(raw_point, (list, tuple)) and len(raw_point) == 2:
                x = finite_number(raw_point[0])
                y = finite_number(raw_point[1])
            else:
                points = []
                break
            if x is None or y is None or x < -2 or y < -2 or x > width + 2 or y > height + 2:
                points = []
                break
            points.append([x, y])
        if len(points) < 3:
            continue

        facet_id = str(raw_facet.get("id") or f"nvidia-facet-{index:03d}")[:100]
        if facet_id in seen_ids:
            facet_id = f"nvidia-facet-{index:03d}"
        seen_ids.add(facet_id)
        pitch: dict[str, Any] | None = None
        raw_pitch = raw_facet.get("pitch")
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
        raw_direction = raw_facet.get("downslopeDirectionPage")
        if isinstance(raw_direction, (list, tuple)) and len(raw_direction) == 2:
            dx = finite_number(raw_direction[0])
            dy = finite_number(raw_direction[1])
            if dx is not None and dy is not None and math.hypot(dx, dy) > 1e-8:
                direction = [dx, dy]
        raw_edges = raw_facet.get("edgeTypes")
        edges = (
            [str(edge) for edge in raw_edges if str(edge) in allowed_edge_types]
            if isinstance(raw_edges, list)
            else []
        )
        if len(edges) != len(points):
            edges = []
        area = finite_number(raw_facet.get("areaLabelSqFt"))
        confidence = finite_number(raw_facet.get("confidence"))
        raw_provenance = raw_facet.get("provenance")
        provenance: dict[str, str] = {}
        for field in ("outline", "pitch", "downslopeDirection", "area"):
            value = raw_provenance.get(field) if isinstance(raw_provenance, dict) else None
            provenance[field] = (
                value
                if isinstance(value, str) and value in {"reported", "inferred"}
                else "reported"
            )
        facets.append(
            {
                "id": facet_id,
                "name": str(raw_facet.get("name") or facet_id)[:120],
                "roofType": raw_facet.get("roofType")
                if raw_facet.get("roofType") in {"flat", "pitched"}
                else "pitched",
                "areaLabelSqFt": area if area is not None and area >= 0 else None,
                "polygonPagePoints": points,
                "pitch": pitch,
                "downslopeDirectionPage": direction,
                "edgeTypes": edges,
                "drawOrder": len(facets),
                "source": "nvidia_visual_extraction",
                "confidence": min(max(confidence, 0.0), 1.0) if confidence is not None else 0.5,
                "provenance": provenance,
            }
        )
    if not facets:
        analysis.setdefault("warnings", []).append(
            "NVIDIA did not return any valid facet polygons in the calibrated PDF page frame."
        )
        return False

    analysis["coordinatePageNumber"] = raw_page
    analysis["coordinatePageSize"] = {"widthPoints": width, "heightPoints": height}
    analysis["facets"] = facets
    analysis["facetCount"] = len(facets)
    analysis["expectedFacetCount"] = measurement.get("roofFacets") or evidence.get(
        "expectedFacetCount"
    )
    analysis["confidence"] = "low"
    analysis["geometrySource"] = "nvidia_visual_extraction"
    warnings = analysis.setdefault("warnings", [])
    warnings.append(
        "Facet outlines, pitch, and direction came from NVIDIA visual extraction "
        "and require review."
    )

    calibration = analysis.get("pageCalibrations")
    calibration = calibration if isinstance(calibration, dict) else {}
    feet_per_point = finite_number(calibration.get("feetPerPoint"))
    raw_origin = calibration.get("originPagePoint")
    if isinstance(raw_origin, (list, tuple)) and len(raw_origin) == 2:
        origin_x = finite_number(raw_origin[0])
        origin_y = finite_number(raw_origin[1])
    else:
        origin_x = origin_y = None
    if origin_x is None or origin_y is None:
        all_x = [point[0] for facet in facets for point in facet["polygonPagePoints"]]
        all_y = [point[1] for facet in facets for point in facet["polygonPagePoints"]]
        origin_x = (min(all_x) + max(all_x)) / 2
        origin_y = (min(all_y) + max(all_y)) / 2
        calibration["originPagePoint"] = [origin_x, origin_y]
    analysis["pageCalibrations"] = calibration
    from app.geometry.workflow import build_review_geometry

    geometry, diagnostics = build_review_geometry(
        facets,
        feet_per_point=feet_per_point,
        origin_page_point=(origin_x, origin_y),
        height_tolerance_feet=settings.geometry_height_tolerance_feet,
        source="assisted",
        confidence="low",
        snap_tolerance_points=settings.geometry_snap_tolerance_points,
        report_name=report_name,
        area_tolerance_percent=settings.geometry_facet_area_tolerance_percent,
        wall_depth_feet=settings.geometry_wall_depth_feet,
    )
    analysis["geometry"] = geometry
    analysis["llmGeometryDiagnostics"] = diagnostics
    if isinstance(analysis.get("validation"), dict) and feet_per_point is not None:
        from app.geometry.validate import summarize_checks, validate_facet_areas

        checks = validate_facet_areas(
            facets,
            feet_per_point=feet_per_point,
            tolerance_percent=settings.geometry_facet_area_tolerance_percent,
        )
        analysis["llmGeometryValidation"] = {
            "checks": checks,
            "summary": summarize_checks(checks),
        }
    warnings.extend(diagnostics.get("warnings", []))
    warnings.extend(diagnostics.get("failures", []))
    analysis["warnings"] = list(dict.fromkeys(warnings))
    return True


def _has_complete_vector_inputs_for_review(analysis: dict[str, Any]) -> bool:
    """Return whether local PDF vectors supplied outlines, scale, pitches, and arrows.

    Height conflicts are a contractor-review issue. They do not make complete source
    vector evidence a reason to replace those vectors with model-generated points.
    """
    geometry = analysis.get("geometry")
    facets = analysis.get("facets")
    if not isinstance(geometry, dict) or not isinstance(facets, list) or not facets:
        return False
    vertices = geometry.get("vertices")
    planes = geometry.get("planes")
    calibration = analysis.get("pageCalibrations")
    if (
        not isinstance(vertices, list)
        or not vertices
        or not isinstance(planes, list)
        or len(planes) != len(facets)
        or not isinstance(calibration, dict)
    ):
        return False

    feet_per_point = calibration.get("feetPerPoint")
    if (
        isinstance(feet_per_point, bool)
        or not isinstance(feet_per_point, (int, float))
        or not math.isfinite(feet_per_point)
        or feet_per_point <= 0
    ):
        return False

    expected_count = analysis.get("expectedFacetCount")
    if (
        isinstance(expected_count, int)
        and not isinstance(expected_count, bool)
        and expected_count > 0
        and len(facets) != expected_count
    ):
        return False

    for facet in facets:
        if not isinstance(facet, dict):
            return False
        pitch = facet.get("pitch")
        direction = facet.get("downslopeDirectionPage")
        if (
            not isinstance(pitch, dict)
            or not isinstance(direction, (list, tuple))
            or len(direction) != 2
        ):
            return False
        try:
            rise = float(pitch.get("rise"))
            run = float(pitch.get("run"))
            dx, dy = float(direction[0]), float(direction[1])
        except (TypeError, ValueError):
            return False
        if (
            not all(math.isfinite(value) for value in (rise, run, dx, dy))
            or rise < 0
            or run <= 0
            or math.hypot(dx, dy) <= 1e-8
        ):
            return False
    return True


def _has_complete_vector_outlines_for_review(analysis: dict[str, Any]) -> bool:
    """Return whether calibrated PDF paths account for every report facet outline."""
    facets = analysis.get("facets")
    calibration = analysis.get("pageCalibrations")
    expected_count = analysis.get("expectedFacetCount")
    if (
        not isinstance(facets, list)
        or not facets
        or not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count <= 0
        or len(facets) != expected_count
        or not isinstance(calibration, dict)
    ):
        return False

    feet_per_point = calibration.get("feetPerPoint")
    if (
        isinstance(feet_per_point, bool)
        or not isinstance(feet_per_point, (int, float))
        or not math.isfinite(feet_per_point)
        or feet_per_point <= 0
    ):
        return False
    inlier_count = calibration.get("inlierCount")
    if (
        isinstance(inlier_count, bool)
        or not isinstance(inlier_count, int)
        or inlier_count < 3
        or calibration.get("hypothesisChosen") not in {"plan", "sloped"}
    ):
        return False

    for facet in facets:
        if not isinstance(facet, dict):
            return False
        points = facet.get("polygonPagePoints")
        if not isinstance(points, list) or len(points) < 3:
            return False
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                return False
            try:
                coordinates = (float(point[0]), float(point[1]))
            except (TypeError, ValueError):
                return False
            if not all(math.isfinite(value) for value in coordinates):
                return False
    return True


def _geometry_settings(settings: Settings) -> dict[str, float]:
    return {
        "snap_tolerance_points": settings.geometry_snap_tolerance_points,
        "scale_tolerance_percent": settings.geometry_scale_tolerance_percent,
        "facet_area_tolerance_percent": settings.geometry_facet_area_tolerance_percent,
        "class_total_tolerance_percent": settings.geometry_edge_class_tolerance_percent,
        "pitch_tolerance_degrees": float(settings.geometry_pitch_tolerance_degrees),
        "height_tolerance_feet": settings.geometry_height_tolerance_feet,
        "wall_depth_feet": settings.geometry_wall_depth_feet,
    }


def _firestore_safe_geometry_analysis(value: Any) -> Any:
    """Keep nested geometry-analysis arrays in a Firestore-compatible shape."""
    return firestore_safe_value(value)


def _restore_geometry_analysis(value: Any) -> Any:
    return restore_firestore_value(value)


def _geometry_response(db: Any, version_id: str, version: dict[str, Any]) -> dict[str, Any]:
    geometry = version.get("geometry")
    artifact_id = version.get("geometryArtifactId")
    geometry_from_artifact = False
    if geometry is None and isinstance(artifact_id, str):
        project_id = str(version.get("projectId", ""))
        project_ref = db.collection("projects").document(project_id)
        artifact_ref = project_ref.collection("artifacts").document(artifact_id)
        artifact = artifact_ref.get().to_dict() or {}
        if (
            artifact.get("ownerUid") != version.get("ownerUid")
            or artifact.get("status") != "complete"
        ):
            raise _geometry_error(
                "geometry_unavailable", "Stored geometry artifact is unavailable.", 503
            )
        output = io.BytesIO()
        read_file_chunks(artifact_ref, output, artifact)
        try:
            geometry = json.loads(output.getvalue())
            geometry_from_artifact = True
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _geometry_error(
                "geometry_unavailable", "Stored geometry artifact is invalid.", 503
            ) from exc
    if isinstance(geometry, dict) and not geometry_from_artifact:
        geometry = restore_firestore_value(geometry)
    if isinstance(geometry, dict):
        from app.geometry.migrations import migrate_geometry

        try:
            geometry = migrate_geometry(geometry)
        except ValueError as exc:
            raise _geometry_error(
                "geometry_unavailable", "Stored geometry uses an unsupported schema.", 503
            ) from exc
        # The version document is authoritative for review/approval lifecycle state.
        if version.get("status") in {"needs_review", "approved", "failed"}:
            geometry["status"] = version["status"]
    response_version = restore_firestore_value(dict(version))
    return cast(
        dict[str, Any], _jsonable({"id": version_id, **response_version, "geometry": geometry})
    )


def _discard_geometry_version(
    db: Any, project_ref: Any, version_ref: Any, settings: Settings
) -> None:
    """Remove an unreferenced review revision after an optimistic-write conflict."""
    prefix = f"geometry_{version_ref.id}_"
    artifacts = project_ref.collection("artifacts")
    for snapshot in artifacts.stream():
        if not snapshot.id.startswith(prefix):
            continue
        artifact_ref = snapshot.reference
        try:
            delete_file_chunks(
                db,
                artifact_ref,
                batch_size=settings.firestore_delete_batch_size,
                retry_attempts=settings.firebase_retry_attempts,
            )
            artifact_ref.delete()
        except Exception as exc:
            logger.warning("Geometry artifact cleanup failed type=%s", type(exc).__name__)
    try:
        delete_file_chunks(
            db,
            version_ref,
            batch_size=settings.firestore_delete_batch_size,
            retry_attempts=settings.firebase_retry_attempts,
        )
        version_ref.delete()
    except Exception as exc:
        logger.warning("Geometry version cleanup failed type=%s", type(exc).__name__)


def _run_geometry_reconstruction(
    job_id: str, project_id: str, report_id: str, version_id: str
) -> None:
    with job_slots:
        db = database()
        settings = current_settings()
        project_ref = db.collection("projects").document(project_id)
        report_ref = project_ref.collection("reports").document(report_id)
        version_ref = project_ref.collection("geometryVersions").document(version_id)
        job_ref = _job_ref(db, job_id)
        scratch = tempfile.mkdtemp(prefix="roofing-geometry-")
        os.chmod(scratch, 0o700)
        pdf_path = Path(scratch) / "source.pdf"
        events: list[dict[str, str]] = []
        failure_stage = "starting geometry reconstruction"
        try:
            queued_job = job_ref.get().to_dict() or {}
            stored_events = queued_job.get("events")
            if isinstance(stored_events, list):
                events = [event for event in stored_events if isinstance(event, dict)]
            job_ref.update(
                {
                    "status": "running",
                    "attempt": 1,
                    "startedAt": _server_time(),
                    "updatedAt": _server_time(),
                }
            )
            _record_job_progress(
                job_ref, events, "read_pdf", "Reading the stored PDF from project storage."
            )
            failure_stage = "reading the stored PDF"
            report = report_ref.get().to_dict() or {}
            with pdf_path.open("wb") as output:
                read_file_chunks(report_ref, output, report)
            from app.geometry.diagram_extract import analyze_pdf_geometry

            failure_stage = "analyzing PDF vectors"
            _record_job_progress(
                job_ref,
                events,
                "analyze_vectors",
                "Reading vector paths, labels, dimensions, and pitch from the PDF.",
            )
            analysis = analyze_pdf_geometry(pdf_path, **_geometry_settings(settings))
            _record_job_progress(
                job_ref,
                events,
                "validate_vectors",
                f"PDF vector analysis found {len(analysis.get('facets', []))} candidate outline(s).",
            )
            version_snapshot = version_ref.get().to_dict() or {}
            measurement_version_id = version_snapshot.get("measurementVersionId")
            measurement: dict[str, Any] = {}
            measurement_ref = None
            if isinstance(measurement_version_id, str):
                measurement_ref = project_ref.collection("measurementVersions").document(
                    measurement_version_id
                )
                measurement = measurement_ref.get().to_dict() or {}
            report_name = str(report.get("originalFilename", "measurement-report.pdf"))
            evidence = measurement.get("geometryEvidence")
            has_llm_facets = (
                isinstance(evidence, dict)
                and isinstance(evidence.get("facets"), list)
                and bool(evidence.get("facets"))
            )
            vector_inputs_complete = _has_complete_vector_inputs_for_review(analysis)
            vector_outlines_complete = _has_complete_vector_outlines_for_review(analysis)
            if vector_outlines_complete and not vector_inputs_complete:
                _record_job_progress(
                    job_ref,
                    events,
                    "vector_outlines_complete",
                    (
                        f"PDF vectors recovered all {len(analysis.get('facets', []))} reported "
                        "roof outlines. Using them for Geometry review; unresolved pitch, "
                        "direction, or height constraints remain."
                    ),
                )
            if not vector_inputs_complete and not vector_outlines_complete and not has_llm_facets:
                failure_stage = "extracting diagram points"
                _record_job_progress(
                    job_ref,
                    events,
                    "diagram_points",
                    "Requesting diagram points from NVIDIA. This remote step can take up to "
                    f"{settings.nvidia_timeout_seconds:g} seconds per request.",
                )
                try:
                    # Roofr's scalar tables are parsed locally. Ask NVIDIA separately
                    # for diagram points so the 3D candidate still comes from the PDF.
                    model_result, _ = call_nvidia(pdf_path, settings)
                    model_geometry = model_result.get("geometry_review")
                    if isinstance(model_geometry, dict):
                        evidence = _safe_geometry_review(model_geometry)
                        if evidence.get("facets"):
                            measurement["geometryEvidence"] = evidence
                            if measurement_ref is not None:
                                try:
                                    measurement_ref.update({"geometryEvidence": evidence})
                                except Exception as exc:
                                    logger.warning(
                                        "Could not persist model geometry evidence type=%s",
                                        type(exc).__name__,
                                    )
                            _record_job_progress(
                                job_ref,
                                events,
                                "diagram_points_complete",
                                "NVIDIA returned diagram vertices; validating them against the PDF evidence.",
                            )
                        else:
                            analysis.setdefault("warnings", []).append(
                                "NVIDIA did not return usable diagram vertices; PDF vector analysis remains available for review."
                            )
                            _record_job_progress(
                                job_ref,
                                events,
                                "diagram_points_unavailable",
                                "NVIDIA returned no usable diagram vertices; continuing with local PDF analysis.",
                                level="warning",
                            )
                except Exception as exc:
                    logger.warning("NVIDIA geometry evidence unavailable type=%s", type(exc).__name__)
                    analysis.setdefault("warnings", []).append(
                        "NVIDIA diagram-point extraction was unavailable; PDF vector analysis remains available for review."
                    )
                    provider_status = getattr(exc, "provider_status", None)
                    provider_note = (
                        f"NVIDIA returned HTTP {provider_status} after retries; "
                        if isinstance(provider_status, int)
                        else "NVIDIA diagram extraction failed; "
                    )
                    _record_job_progress(
                        job_ref,
                        events,
                        "diagram_points_unavailable",
                        f"{provider_note}continuing with local PDF analysis.",
                        level="warning",
                    )
            if vector_inputs_complete or vector_outlines_complete:
                used_llm_geometry = False
                if has_llm_facets:
                    analysis.setdefault("warnings", []).append(
                        "Complete calibrated PDF outlines and pitch data produced the active "
                        "roof model; assisted diagram points were retained as evidence."
                        if vector_inputs_complete
                        else "Complete calibrated PDF outlines match the report's facet count; "
                        "assisted diagram points were retained as evidence while unresolved "
                        "pitch and height constraints remain in Geometry review."
                    )
                _record_job_progress(
                    job_ref,
                    events,
                    "use_vector_geometry",
                    (
                        (
                            "Using complete calibrated PDF outlines, pitches, and direction "
                            "arrows; relative roof heights are solved."
                            if isinstance(analysis.get("geometry"), dict)
                            and isinstance(analysis["geometry"].get("heightSolve"), dict)
                            and analysis["geometry"]["heightSolve"].get("status") == "solved"
                            else "Using complete calibrated PDF outlines, pitches, and direction "
                            "arrows; shared-edge height conflicts remain for Geometry review."
                        )
                        if vector_inputs_complete
                        else (
                            f"Using all {len(analysis.get('facets', []))} calibrated PDF roof "
                            "outlines; unresolved pitch, direction, or height constraints remain "
                            "in Geometry review."
                        )
                    ),
                )
            else:
                used_llm_geometry = _apply_llm_geometry_evidence(
                    analysis, measurement, settings, report_name
                )
                _record_job_progress(
                    job_ref,
                    events,
                    "use_assisted_geometry" if used_llm_geometry else "vector_geometry_incomplete",
                    (
                        "Using assisted diagram points because the local vector model is "
                        "incomplete or unsolved."
                        if used_llm_geometry
                        else "Local PDF analysis did not produce a complete solved roof model; "
                        "review the available outlines and validation warnings."
                    ),
                    level="info" if used_llm_geometry else "warning",
                )
            failure_stage = "preparing geometry and validation data"
            geometry = analysis.pop("geometry", None)
            validation = analysis.get("validation")
            validation_checks = (
                list(validation.get("checks", [])) if isinstance(validation, dict) else []
            )
            llm_validation = analysis.get("llmGeometryValidation")
            if isinstance(llm_validation, dict):
                validation_checks.extend(llm_validation.get("checks", []))
            version_data = cast(
                dict[str, Any],
                firestore_safe_value(
                    {
                        "status": "needs_review",
                        "source": "assisted" if used_llm_geometry else "extracted",
                        "confidence": analysis.get("confidence", "low"),
                        "estimatedHeights": True,
                        "analysis": analysis,
                        "calibration": analysis.get("pageCalibrations"),
                        "validationResults": validation_checks,
                        "sourceReportId": report_id,
                        "reportFilename": report_name,
                        "updatedAt": _server_time(),
                    }
                ),
            )
            if geometry:
                _record_job_progress(
                    job_ref,
                    events,
                    "save_geometry",
                    "Saving the reviewable roof draft and its validation results.",
                )
                geometry["sourceReference"]["report"] = report_name
                failure_stage = "writing the geometry payload to Firestore"
                store_geometry_version(
                    db,
                    project_ref,
                    version_ref,
                    geometry,
                    owner_uid=str((version_ref.get().to_dict() or {}).get("ownerUid", "")),
                    settings=settings,
                )
                # store_geometry_version owns the inline geometry or chunk artifact field.
            else:
                _record_job_progress(
                    job_ref,
                    events,
                    "save_analysis",
                    "Saving the PDF analysis and any candidate outlines for review.",
                )
            failure_stage = "writing geometry review metadata to Firestore"
            version_ref.update(version_data)
            failure_stage = "updating project geometry status"
            project_ref.update({"geometryStatus": "needs_review", "updatedAt": _server_time()})
            _record_job_progress(
                job_ref,
                events,
                "complete",
                "Geometry processing finished. The draft is ready for review.",
            )
            failure_stage = "marking the geometry job complete"
            job_ref.update(
                {
                    "status": "succeeded",
                    "resultRefs": {"geometryVersionId": version_id},
                    "finishedAt": _server_time(),
                    "updatedAt": _server_time(),
                }
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())[:240]
            logger.exception(
                "Geometry reconstruction failed stage=%s type=%s",
                failure_stage,
                type(exc).__name__,
            )
            failure_message = (
                f"Geometry processing failed during {failure_stage} ({type(exc).__name__})."
            )
            if detail:
                failure_message = f"{failure_message} {detail}"
            _record_job_progress(
                job_ref,
                events,
                "failed",
                failure_message,
                level="error",
            )
            version_ref.update(
                {
                    "status": "failed",
                    "error": {
                        "code": "geometry_processing_failed",
                        "message": "The report geometry could not be processed.",
                    },
                    "updatedAt": _server_time(),
                }
            )
            project_ref.update({"geometryStatus": "failed", "updatedAt": _server_time()})
            job_ref.update(
                {
                    "status": "failed",
                    "error": {
                        "code": "geometry_processing_failed",
                        "message": "The report geometry could not be processed.",
                    },
                    "finishedAt": _server_time(),
                    "updatedAt": _server_time(),
                }
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _queue_geometry_reconstruction(
    db: Any,
    project_ref: Any,
    project_id: str,
    owner_uid: str,
    report_id: str,
    *,
    force: bool,
) -> dict[str, str]:
    """Create a versioned geometry job, deduplicating automatic retries."""

    def queue_geometry(tx: Any) -> dict[str, str]:
        current_project = project_ref.get(transaction=tx).to_dict() or {}
        if current_project.get("ownerUid") != owner_uid:
            raise _geometry_error("not_found", "Project not found.", 404)
        current_id = current_project.get("currentGeometryVersionId")
        current: dict[str, Any] = {}
        if isinstance(current_id, str):
            current = (
                project_ref.collection("geometryVersions")
                .document(current_id)
                .get(transaction=tx)
                .to_dict()
                or {}
            )
            same_source = (
                current.get("sourceReportId") == report_id
                and current.get("measurementVersionId")
                == current_project.get("currentMeasurementVersionId")
            )
            if current.get("status") == "processing":
                if not same_source:
                    raise _geometry_error(
                        "geometry_processing",
                        "A different report is already being reconstructed. "
                        "Wait for it to finish, then retry.",
                        409,
                    )
                return {
                    "status": "processing",
                    "jobId": str(current.get("jobId", "")),
                    "geometryVersionId": current_id,
                }
            if not force and same_source and current.get("status") in {"needs_review", "approved"}:
                return {
                    "status": str(current.get("status")),
                    "jobId": str(current.get("jobId", "")),
                    "geometryVersionId": current_id,
                }
        version_id = f"g_{uuid.uuid4().hex}"
        job_id = f"j_g_{uuid.uuid4().hex}"
        version_ref = project_ref.collection("geometryVersions").document(version_id)
        version = int(current_project.get("geometryVersionCounter", 0)) + 1
        now = _server_time()
        queued_at = datetime.now(UTC).isoformat()
        queued_event = {
            "stage": "queued",
            "message": "Geometry reconstruction is queued.",
            "level": "info",
            "at": queued_at,
        }
        tx.set(
            version_ref,
            {
                "ownerUid": owner_uid,
                "projectId": project_id,
                "sourceReportId": report_id,
                "measurementVersionId": current_project.get("currentMeasurementVersionId"),
                "status": "processing",
                "analysis": {
                    "status": "processing",
                    "sourceFormat": "unknown",
                    "facets": [],
                    "warnings": [],
                },
                "source": "extracted",
                "confidence": "low",
                "estimatedHeights": True,
                "version": version,
                "basedOnVersionId": current_id,
                "jobId": job_id,
                "createdAt": now,
                "updatedAt": now,
            },
        )
        tx.set(
            _job_ref(db, job_id),
            {
                "ownerUid": owner_uid,
                "projectId": project_id,
                "reportId": report_id,
                "geometryVersionId": version_id,
                "type": "geometry_reconstruction",
                "status": "queued",
                "attempt": 0,
                "progress": {
                    "stage": "queued",
                    "message": queued_event["message"],
                    "at": queued_at,
                },
                "events": [queued_event],
                "createdAt": now,
                "updatedAt": now,
            },
        )
        tx.update(
            project_ref,
            {
                "currentGeometryVersionId": version_id,
                "geometryVersionCounter": version,
                "geometryStatus": "processing",
                "updatedAt": now,
            },
        )
        return {"status": "queued", "jobId": job_id, "geometryVersionId": version_id}

    return cast(dict[str, str], _run_transaction(db, queue_geometry))


@app.post("/api/projects/{project_id}/geometry/reconstruct", status_code=202)
def reconstruct_geometry(
    project_id: str,
    background_tasks: BackgroundTasks,
    payload: GeometryReconstructRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    report_id = (payload.reportId if payload else None) or project.get("currentReportId")
    if not isinstance(report_id, str) or not report_id:
        raise _geometry_error("report_missing", "Upload a complete measurement report first.", 404)
    _read_geometry_report(project_ref, report_id, user_id(user))
    queued = _queue_geometry_reconstruction(
        db, project_ref, project_id, user_id(user), report_id, force=True
    )
    if queued["status"] == "queued":
        background_tasks.add_task(
            _run_geometry_reconstruction,
            queued["jobId"],
            project_id,
            report_id,
            queued["geometryVersionId"],
        )
    return queued


@app.get("/api/projects/{project_id}/geometry")
def current_geometry(
    project_id: str,
    version_id: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    current_id = project.get("currentGeometryVersionId")
    selected_id = version_id or current_id
    if not isinstance(selected_id, str) or not selected_id:
        raise _geometry_error("geometry_missing", "No geometry version is available yet.", 404)
    version_ref = project_ref.collection("geometryVersions").document(selected_id)
    version = version_ref.get().to_dict()
    if not version or version.get("ownerUid") != user_id(user):
        raise _geometry_error("geometry_missing", "No geometry version is available yet.", 404)
    return {**_geometry_response(db, selected_id, version), "isCurrent": selected_id == current_id}


@app.get("/api/projects/{project_id}/geometry/versions")
def geometry_versions(
    project_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> list[dict[str, Any]]:
    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    current_id = project.get("currentGeometryVersionId")
    versions: list[dict[str, Any]] = []
    for snapshot in project_ref.collection("geometryVersions").stream():
        version = snapshot.to_dict() or {}
        if version.get("ownerUid") != user_id(user):
            continue
        versions.append(
            {
                "id": snapshot.id,
                "version": version.get("version"),
                "status": version.get("status"),
                "source": version.get("source"),
                "confidence": version.get("confidence"),
                "createdAt": version.get("createdAt"),
                "isCurrent": snapshot.id == current_id,
            }
        )
    versions.sort(key=lambda item: int(item.get("version") or 0), reverse=True)
    return cast(list[dict[str, Any]], _jsonable(versions))


@app.get("/api/projects/{project_id}/geometry/page-image")
def geometry_page_image(
    project_id: str,
    page: int = 1,
    version_id: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> Response:
    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    selected_version_id = version_id or project.get("currentGeometryVersionId")
    version = (
        project_ref.collection("geometryVersions").document(selected_version_id).get().to_dict()
        if selected_version_id
        else None
    )
    if version_id and (not version or version.get("ownerUid") != user_id(user)):
        raise _geometry_error("geometry_missing", "Geometry version not found.", 404)
    report_id = (version or {}).get("sourceReportId") or project.get("currentReportId")
    if not isinstance(report_id, str) or not report_id:
        raise _geometry_error("report_missing", "Measurement report not found.", 404)
    report_ref, report = _read_geometry_report(project_ref, report_id, user_id(user))
    if page < 1 or page > settings.max_pdf_pages:
        raise _geometry_error(
            "invalid_page", "Requested PDF page is outside the configured limit.", 422
        )
    scratch = tempfile.mkdtemp(prefix="roofing-page-image-")
    os.chmod(scratch, 0o700)
    pdf_path = Path(scratch) / "source.pdf"
    try:
        with pdf_path.open("wb") as output:
            read_file_chunks(report_ref, output, report)
        import fitz

        with fitz.open(pdf_path) as document:
            if page > len(document):
                raise _geometry_error(
                    "invalid_page", "Requested page does not exist in this report.", 404
                )
            pixmap = document[page - 1].get_pixmap(
                dpi=settings.geometry_page_render_dpi, alpha=False
            )
            content = pixmap.tobytes("png")
        return Response(
            content=content,
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _geometry_3d_edit_facets(
    db: Any, current_id: str, current: dict[str, Any], payload: Geometry3DEditRequest
) -> list[Any]:
    """Translate feet-based 3D editor values into the PDF-point review format."""
    from app.schemas import GeometryFacetEdit

    response = _geometry_response(db, current_id, current)
    geometry = response.get("geometry")
    calibration = current.get("calibration")
    if not isinstance(calibration, dict):
        analysis = current.get("analysis")
        calibration = analysis.get("pageCalibrations") if isinstance(analysis, dict) else None
    feet_per_point = calibration.get("feetPerPoint") if isinstance(calibration, dict) else None
    origin = calibration.get("originPagePoint") if isinstance(calibration, dict) else None
    if (
        not isinstance(geometry, dict)
        or not isinstance(feet_per_point, (int, float))
        or isinstance(feet_per_point, bool)
        or not math.isfinite(float(feet_per_point))
        or float(feet_per_point) <= 0
        or not isinstance(origin, (list, tuple))
        or len(origin) != 2
    ):
        raise _geometry_error(
            "geometry_scale_unavailable",
            "This draft has no calibrated PDF scale. Set a known scale in Geometry review before editing it in 3D.",
            422,
        )
    scale = float(feet_per_point)
    origin_x, origin_y = float(origin[0]), float(origin[1])
    vertices = {vertex.id: vertex.plan for vertex in payload.vertices}
    edge_types = {
        frozenset(edge.get("vertexIds", [])): str(edge.get("type", "other"))
        for edge in geometry.get("edges", [])
        if isinstance(edge, dict) and isinstance(edge.get("vertexIds"), (list, tuple))
    }
    geometry_planes = {
        plane.get("id"): plane
        for plane in geometry.get("planes", [])
        if isinstance(plane, dict)
    }
    facets = []
    for plane in payload.planes:
        try:
            plan_points = [vertices[vertex_id] for vertex_id in plane.boundaryVertexIds]
        except KeyError as exc:
            raise _geometry_error(
                "invalid_geometry_vertex", "A plane references a missing vertex.", 422
            ) from exc
        page_points = [(u / scale + origin_x, origin_y - v / scale) for u, v in plan_points]
        dx, dv = plane.downslopePlanUnitVector
        previous_plane = geometry_planes.get(plane.id, {})
        previous_edge_types = (
            previous_plane.get("edgeTypes") if isinstance(previous_plane, dict) else None
        )
        plane_edges: list[str] = []
        for index, first_id in enumerate(plane.boundaryVertexIds):
            second_id = plane.boundaryVertexIds[(index + 1) % len(plane.boundaryVertexIds)]
            edge_type = edge_types.get(frozenset((first_id, second_id)))
            if edge_type is None and isinstance(previous_edge_types, list) and index < len(previous_edge_types):
                edge_type = str(previous_edge_types[index])
            plane_edges.append(edge_type or "other")
        facets.append(
            GeometryFacetEdit.model_validate(
                {
                    "id": plane.id,
                    "name": plane.name,
                    "roofType": plane.roofType,
                    "polygonPagePoints": page_points,
                    "pitch": plane.pitch.model_dump(mode="json"),
                    "downslopeDirectionPage": (dx, -dv),
                    "edgeTypes": plane_edges,
                    "reportedAreaSqFt": plane.reportedAreaSqFt,
                    "drawOrder": plane.drawOrder,
                    "confidence": 0.5,
                    "provenance": {
                        "outline": "user_edited",
                        "pitch": "user_edited",
                        "downslopeDirection": "user_edited",
                        "area": "user_edited",
                    },
                }
            )
        )
    return facets


@app.put("/api/projects/{project_id}/geometry")
def save_geometry_review(
    project_id: str,
    payload: GeometryEditRequest | Geometry3DEditRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    from app.geometry.diagram_extract import analyze_pdf_geometry, read_vector_page
    from app.geometry.validate import validate_review_geometry
    from app.geometry.workflow import build_review_geometry

    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    current_id = project.get("currentGeometryVersionId")
    if current_id != payload.expectedCurrentVersionId:
        raise _geometry_error(
            "version_conflict", "Geometry changed in another session. Reload before saving.", 409
        )
    current_ref = project_ref.collection("geometryVersions").document(current_id)
    current = current_ref.get().to_dict() or {}
    if current.get("ownerUid") != user_id(user):
        raise _geometry_error("not_found", "Geometry version not found.", 404)
    if int(current.get("version", 0)) != payload.expectedVersion:
        raise _geometry_error(
            "version_conflict", "Geometry version changed in another session.", 409
        )
    if current.get("status") == "processing":
        raise _geometry_error(
            "geometry_processing", "Wait for the geometry job to finish before editing.", 409
        )
    is_3d_edit = isinstance(payload, Geometry3DEditRequest)
    base_version_id = (
        payload.basedOnVersionId
        if isinstance(payload, GeometryEditRequest) and payload.basedOnVersionId
        else current_id
    )
    base_version_ref = project_ref.collection("geometryVersions").document(base_version_id)
    base_version = base_version_ref.get().to_dict() or {}
    if base_version.get("ownerUid") != user_id(user):
        raise _geometry_error("not_found", "Geometry version not found.", 404)
    if base_version.get("status") == "processing":
        raise _geometry_error(
            "geometry_processing",
            "Wait for the selected geometry job to finish before editing.",
            409,
        )
    if not is_3d_edit and payload.manualFeetPerPoint is not None and not payload.confirmManualScale:
        raise _geometry_error(
            "scale_confirmation_required",
            "Confirm that the manual scale comes from a known report dimension.",
            422,
        )
    report_id = base_version.get("sourceReportId")
    if not isinstance(report_id, str):
        raise _geometry_error("report_missing", "Geometry source report is unavailable.", 404)
    report_ref, report = _read_geometry_report(project_ref, report_id, user_id(user))
    if is_3d_edit:
        edit_facets = _geometry_3d_edit_facets(db, current_id, current, payload)
        manual_feet_per_point = None
        confirm_manual_scale = False
        calibration = current.get("calibration")
        if not isinstance(calibration, dict):
            current_analysis = current.get("analysis")
            calibration = (
                current_analysis.get("pageCalibrations")
                if isinstance(current_analysis, dict)
                else None
            )
        if isinstance(calibration, dict) and calibration.get("manualScaleConfirmed") is True:
            calibrated_scale = calibration.get("feetPerPoint")
            if isinstance(calibrated_scale, (int, float)) and not isinstance(calibrated_scale, bool):
                manual_feet_per_point = Decimal(str(calibrated_scale))
                confirm_manual_scale = True
    else:
        edit_facets = payload.facets
        manual_feet_per_point = payload.manualFeetPerPoint
        confirm_manual_scale = payload.confirmManualScale
    scratch = tempfile.mkdtemp(prefix="roofing-geometry-review-")
    os.chmod(scratch, 0o700)
    pdf_path = Path(scratch) / "source.pdf"
    try:
        with pdf_path.open("wb") as output:
            read_file_chunks(report_ref, output, report)
        source_analysis = analyze_pdf_geometry(pdf_path, **_geometry_settings(settings))
        original_analysis = base_version.get("analysis")
        if isinstance(original_analysis, dict):
            # The source extraction is refreshed from the immutable PDF on every save.
            source_analysis.setdefault("sourceFormat", original_analysis.get("sourceFormat"))
        if manual_feet_per_point is None:
            prior_calibration = base_version.get("calibration")
            if not isinstance(prior_calibration, dict) and isinstance(original_analysis, dict):
                prior_calibration = original_analysis.get("pageCalibrations")
            if isinstance(prior_calibration, dict):
                source_analysis["pageCalibrations"] = dict(prior_calibration)
        base_facets = source_analysis.get("facets", [])
        base_by_id = {
            str(facet.get("id")): facet for facet in base_facets if isinstance(facet, dict)
        }
        requested_facets = [facet.model_dump(mode="json") for facet in edit_facets]
        selected_analysis = base_version.get("analysis")
        selected_facets = selected_analysis.get("facets", []) if isinstance(selected_analysis, dict) else []
        selected_facet_ids = {
            str(facet.get("id")) for facet in selected_facets if isinstance(facet, dict)
        }
        requested_facet_ids = {str(facet.get("id")) for facet in requested_facets}
        topology_edited = requested_facet_ids != selected_facet_ids
        user_added_facet_ids = {
            str(facet.get("id"))
            for facet in selected_facets
            if isinstance(facet, dict) and facet.get("source") == "user_traced"
        } | (requested_facet_ids - selected_facet_ids)
        seen_ids: set[str] = set()
        merged_facets: list[dict[str, Any]] = []
        for edited in requested_facets:
            facet_id = str(edited["id"])
            if facet_id in seen_ids:
                raise _geometry_error("duplicate_plane", "Each plane ID must be unique.", 422)
            seen_ids.add(facet_id)
            prior = dict(base_by_id.get(facet_id, {}))
            edited_pitch = edited.get("pitch")
            if isinstance(edited_pitch, dict):
                edited_pitch = {
                    "notation": str(edited_pitch.get("notation", "")),
                    "rise": float(edited_pitch.get("rise", 0)),
                    "run": float(edited_pitch.get("run", 0)),
                }
            prior.update(
                {
                    "id": facet_id,
                    "name": edited["name"],
                    "roofType": edited["roofType"],
                    "polygonPagePoints": edited["polygonPagePoints"],
                    "pitch": edited_pitch,
                    "downslopeDirectionPage": edited.get("downslopeDirectionPage"),
                    "edgeTypes": edited.get("edgeTypes", []),
                    "drawOrder": (
                        edited.get("drawOrder")
                        if isinstance(edited.get("drawOrder"), int)
                        and not isinstance(edited.get("drawOrder"), bool)
                        else len(merged_facets)
                    ),
                }
            )
            if facet_id in user_added_facet_ids:
                prior["source"] = "user_traced"
            if edited.get("reportedAreaSqFt") is not None:
                prior["areaLabelSqFt"] = float(edited["reportedAreaSqFt"])
            if edited.get("confidence") is not None:
                prior["confidence"] = float(edited["confidence"])
            if edited.get("provenance"):
                prior["provenance"] = edited["provenance"]
            merged_facets.append(prior)
        source_analysis["facets"] = merged_facets
        source_analysis["facetCount"] = len(merged_facets)
        source_analysis["stackingOrderConfirmed"] = payload.confirmStackingOrder
        calibration = source_analysis.get("pageCalibrations")
        if not isinstance(calibration, dict):
            calibration = {}
            source_analysis["pageCalibrations"] = calibration
        if manual_feet_per_point is not None:
            calibration["feetPerPoint"] = float(manual_feet_per_point)
            calibration.update(
                {
                    "method": "Contractor-calibrated scale from a known report dimension",
                    "hypothesisChosen": "manual",
                    "manualScaleConfirmed": True,
                }
            )
        feet_per_point = calibration.get("feetPerPoint") if isinstance(calibration, dict) else None
        scale_value = (
            float(feet_per_point)
            if isinstance(feet_per_point, (int, float))
            and math.isfinite(float(feet_per_point))
            and float(feet_per_point) > 0
            else None
        )
        points = [point for facet in merged_facets for point in facet.get("polygonPagePoints", [])]
        page_size = source_analysis.get("coordinatePageSize")
        if isinstance(page_size, dict):
            width = float(page_size.get("widthPoints", 0))
            height = float(page_size.get("heightPoints", 0))
            outside_page = any(
                float(point[0]) < -settings.geometry_snap_tolerance_points
                or float(point[1]) < -settings.geometry_snap_tolerance_points
                or float(point[0]) > width + settings.geometry_snap_tolerance_points
                or float(point[1]) > height + settings.geometry_snap_tolerance_points
                for facet in merged_facets
                for point in facet.get("polygonPagePoints", [])
            )
            if outside_page:
                raise _geometry_error(
                    "invalid_geometry_coordinate",
                    "Every plane vertex must remain within its source PDF page.",
                    422,
                )
        if isinstance(calibration, dict) and calibration.get("originPagePoint"):
            origin = cast(tuple[float, float], tuple(calibration["originPagePoint"]))
        elif points:
            origin = (
                min(float(point[0]) for point in points),
                max(float(point[1]) for point in points),
            )
            if isinstance(calibration, dict):
                calibration["originPagePoint"] = list(origin)
        else:
            origin = (0.0, 0.0)

        geometry = None
        build_diagnostics: dict[str, Any] = {"warnings": [], "failures": []}
        page_objects: list[Any] = []
        import fitz

        with fitz.open(pdf_path) as document:
            page_objects = [
                read_vector_page(item, index + 1) for index, item in enumerate(document)
            ]
        length_page = next(
            (item for item in page_objects if "length measurement report" in item.text.lower()),
            None,
        )
        area_page = next(
            (item for item in page_objects if "area measurement report" in item.text.lower()), None
        )
        edge_classifier = None
        if (
            length_page
            and area_page
            and scale_value is not None
            and isinstance(calibration, dict)
            and calibration.get("lengthToArea")
        ):
            from app.geometry.roofr import _dimension_segments

            transform = calibration["lengthToArea"]
            length_segments = _dimension_segments(length_page)

            def classify_edge(start: tuple[float, float], end: tuple[float, float]) -> str:
                page_start = (
                    start[0] / scale_value + origin[0],
                    origin[1] - start[1] / scale_value,
                )
                page_end = (
                    end[0] / scale_value + origin[0],
                    origin[1] - end[1] / scale_value,
                )
                length_start = (
                    (page_start[0] - float(transform["translate_x"])) / float(transform["scale"]),
                    (page_start[1] - float(transform["translate_y"])) / float(transform["scale"]),
                )
                length_end = (
                    (page_end[0] - float(transform["translate_x"])) / float(transform["scale"]),
                    (page_end[1] - float(transform["translate_y"])) / float(transform["scale"]),
                )
                midpoint = (
                    (length_start[0] + length_end[0]) / 2,
                    (length_start[1] + length_end[1]) / 2,
                )
                angle = math.atan2(length_end[1] - length_start[1], length_end[0] - length_start[0])
                options: list[tuple[float, str]] = []
                for line in length_segments:
                    if not line.edge_class:
                        continue
                    other_mid = (
                        (line.start[0] + line.end[0]) / 2,
                        (line.start[1] + line.end[1]) / 2,
                    )
                    other_angle = math.atan2(
                        line.end[1] - line.start[1], line.end[0] - line.start[0]
                    )
                    distance = math.dist(midpoint, other_mid)
                    angle_delta = abs((angle - other_angle + math.pi / 2) % math.pi - math.pi / 2)
                    if distance <= 5 and angle_delta <= math.radians(8):
                        options.append((distance + angle_delta * 10, line.edge_class))
                return min(options, default=(float("inf"), "other"))[1]

            edge_classifier = classify_edge
        geometry, build_diagnostics = build_review_geometry(
            merged_facets,
            feet_per_point=scale_value,
            origin_page_point=(float(origin[0]), float(origin[1])),
            height_tolerance_feet=settings.geometry_height_tolerance_feet,
            source="manual",
            confidence="low" if source_analysis.get("sourceFormat") == "roof_manager" else "medium",
            snap_tolerance_points=settings.geometry_snap_tolerance_points,
            edge_classifier=edge_classifier,
            report_name=str(report.get("originalFilename", "measurement-report.pdf")),
            area_tolerance_percent=settings.geometry_facet_area_tolerance_percent,
            wall_depth_feet=settings.geometry_wall_depth_feet,
            stacking_order_confirmed=payload.confirmStackingOrder,
        )
        source_analysis["validation"] = source_analysis.get("validation", {})
        source_analysis["validation"]["heightSolve"] = build_diagnostics.get("heightSolve")
        source_analysis["validation"]["buildDiagnostics"] = build_diagnostics
        checks = validate_review_geometry(
            source_analysis,
            merged_facets,
            geometry,
            feet_per_point=scale_value,
            scale_tolerance_percent=settings.geometry_scale_tolerance_percent,
            area_tolerance_percent=settings.geometry_facet_area_tolerance_percent,
            edge_tolerance_percent=settings.geometry_edge_class_tolerance_percent,
            pitch_tolerance_degrees=float(settings.geometry_pitch_tolerance_degrees),
            manual_scale_confirmed=confirm_manual_scale and manual_feet_per_point is not None,
            topology_edited=topology_edited,
            user_added_facet_ids=user_added_facet_ids,
        )
        from app.geometry.validate import summarize_checks

        source_analysis["validation"]["checks"] = checks
        source_analysis["validation"]["summary"] = summarize_checks(checks)
        source_analysis["warnings"] = list(
            dict.fromkeys(
                source_analysis.get("warnings", [])
                + build_diagnostics.get("warnings", [])
                + build_diagnostics.get("failures", [])
            )
        )
        version_id = f"g_{uuid.uuid4().hex}"
        version_number = int(current.get("version", 1)) + 1
        version_ref = project_ref.collection("geometryVersions").document(version_id)
        source_analysis.pop("geometry", None)
        data: dict[str, Any] = {
            "ownerUid": user_id(user),
            "projectId": project_id,
            "sourceReportId": report_id,
            "measurementVersionId": base_version.get("measurementVersionId")
            or project.get("currentMeasurementVersionId"),
            "status": "needs_review",
            "source": "manual",
            "confidence": "low"
            if source_analysis.get("sourceFormat") == "roof_manager"
            else "medium",
            "estimatedHeights": True,
            "version": version_number,
            "basedOnVersionId": base_version_id,
            "analysis": _firestore_safe_geometry_analysis(source_analysis),
            "calibration": source_analysis.get("pageCalibrations"),
            "validationResults": checks,
            "createdAt": _server_time(),
            "updatedAt": _server_time(),
        }
        version_ref.create(data)
        try:
            if geometry:
                store_geometry_version(
                    db,
                    project_ref,
                    version_ref,
                    geometry,
                    owner_uid=user_id(user),
                    settings=settings,
                )
        except Exception:
            _discard_geometry_version(db, project_ref, version_ref, settings)
            raise
        def commit_geometry_revision(transaction: Any) -> None:
            latest_project = project_ref.get(transaction=transaction).to_dict() or {}
            latest_current_ref = project_ref.collection("geometryVersions").document(current_id)
            latest_current = latest_current_ref.get(transaction=transaction).to_dict() or {}
            if (
                latest_project.get("ownerUid") != user_id(user)
                or latest_project.get("currentGeometryVersionId") != current_id
                or int(latest_current.get("version", 0)) != payload.expectedVersion
            ):
                raise _geometry_error(
                    "version_conflict",
                    "Geometry changed while this revision was being saved.",
                    409,
                )
            final_version = version_ref.get(transaction=transaction).to_dict() or data
            transaction.set(version_ref, final_version)
            transaction.update(
                project_ref,
                {
                    "currentGeometryVersionId": version_id,
                    "geometryVersionCounter": version_number,
                    "geometryStatus": "needs_review",
                    "updatedAt": _server_time(),
                },
            )

        try:
            _run_transaction(db, commit_geometry_revision)
        except HTTPException:
            _discard_geometry_version(db, project_ref, version_ref, settings)
            raise
        except Exception as exc:
            _discard_geometry_version(db, project_ref, version_ref, settings)
            logger.warning("Geometry revision commit failed type=%s", type(exc).__name__)
            raise _geometry_error(
                "geometry_write_failed", "Geometry revision could not be saved.", 503
            ) from exc
        return _geometry_response(db, version_id, version_ref.get().to_dict() or {})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@app.post("/api/projects/{project_id}/geometry/approve")
def approve_geometry(
    project_id: str,
    payload: GeometryApproveRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    from app.geometry.workflow import approval_blockers

    project_ref = owned_project(db, user_id(user), project_id)
    project = project_ref.get().to_dict() or {}
    version_id = project.get("currentGeometryVersionId")
    if not isinstance(version_id, str):
        raise _geometry_error(
            "geometry_missing", "No geometry version is available to approve.", 404
        )
    version_ref = project_ref.collection("geometryVersions").document(version_id)
    version = version_ref.get().to_dict() or {}
    if version.get("ownerUid") != user_id(user):
        raise _geometry_error("not_found", "Geometry version not found.", 404)
    if int(version.get("version", 0)) != payload.expectedVersion:
        raise _geometry_error("version_conflict", "Geometry changed in another session.", 409)
    if not payload.confirmation:
        raise _geometry_error(
            "confirmation_required", "Explicit approval confirmation is required.", 422
        )
    analysis_value = version.get("analysis")
    analysis = (
        _restore_geometry_analysis(analysis_value)
        if isinstance(analysis_value, dict)
        else {}
    )
    if version.get("status") == "approved":
        return _geometry_response(db, version_id, version)
    if version.get("status") != "needs_review":
        raise _geometry_error(
            "geometry_not_reviewable", "Geometry must finish processing before approval.", 409
        )
    response = _geometry_response(db, version_id, version)
    geometry_value = response.get("geometry")
    geometry = geometry_value if isinstance(geometry_value, dict) else None
    blockers = approval_blockers(cast(dict[str, Any], analysis), geometry)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approval_blocked",
                "message": "Geometry still needs review.",
                "blockers": blockers,
            },
        )
    def approve_reviewed_geometry(transaction: Any) -> None:
        latest_project = project_ref.get(transaction=transaction).to_dict() or {}
        latest_version = version_ref.get(transaction=transaction).to_dict() or {}
        if (
            latest_project.get("ownerUid") != user_id(user)
            or latest_project.get("currentGeometryVersionId") != version_id
            or int(latest_version.get("version", 0)) != payload.expectedVersion
            or latest_version.get("status") != "needs_review"
        ):
            raise _geometry_error(
                "version_conflict", "Geometry changed while approval was being recorded.", 409
            )
        now = _server_time()
        transaction.update(
            version_ref,
            {
                "status": "approved",
                "approvedByUid": user_id(user),
                "approvedAt": now,
                "updatedAt": now,
            },
        )
        transaction.update(project_ref, {"geometryStatus": "approved", "updatedAt": now})

    _run_transaction(db, approve_reviewed_geometry)
    return _geometry_response(db, version_id, version_ref.get().to_dict() or {})


@app.get("/api/materials")
def list_materials(
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> list[dict[str, Any]]:
    ref = db.collection("users").document(user_id(user)).collection("materialCatalog")
    return [{"id": row.id, **(row.to_dict() or {})} for row in ref.stream()]


DEFAULT_MATERIAL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "default-asphalt-shingles",
        "name": "Asphalt shingles",
        "category": "Roof Shingles",
        "description": (
            "Roof shingles are a versatile, popular, and cost-effective roofing solution. "
            "Asphalt shingles are an affordable, budget-friendly option that is easy to install. "
            "They provide reasonable durability, may need periodic maintenance, and can be "
            "susceptible to weather damage. Their light weight usually means no special framing "
            "is required."
        ),
        "expectedLifespan": "15–30 years",
        "unit": "bundle",
    },
    {
        "id": "default-fibreglass-shingles",
        "name": "Fibreglass shingles",
        "category": "Roof Shingles",
        "description": (
            "Fibreglass shingles are lightweight, fire-resistant, and easy to install. "
            "Their benefits can justify a price slightly above asphalt shingles. Their light "
            "weight usually means no special framing is required."
        ),
        "expectedLifespan": "20–30 years",
        "unit": "bundle",
    },
    {
        "id": "default-composite-shingles",
        "name": "Composite shingles",
        "category": "Roof Shingles",
        "description": (
            "Composite shingles combine materials such as asphalt and fibreglass for increased "
            "durability. Their enhanced resilience contributes to a higher price point than "
            "traditional asphalt shingles. Their light weight usually means no special framing "
            "is required."
        ),
        "expectedLifespan": "30–50 years",
        "unit": "bundle",
    },
    {
        "id": "default-roll-roofing",
        "name": "Roll roofing",
        "category": "Roll Roofing",
        "description": (
            "A practical, budget-conscious choice for low-slope roofs and sheds. Roll roofing is "
            "lightweight and flexible, and is easy to install for DIY projects or professionals. "
            "It usually does not require special framing. Its shorter service life can mean more "
            "frequent maintenance and replacement."
        ),
        "expectedLifespan": "5–10 years",
        "unit": "roll",
    },
    {
        "id": "default-standing-seam-metal",
        "name": "Standing-seam metal roofing",
        "category": "Metal Roofing",
        "description": (
            "Metal roofing is lightweight and low maintenance. Standing-seam roofing has a sleek "
            "appearance and exceptional durability. Its concealed fasteners reduce the chance of "
            "leaks, and it is weather resistant. Installation may require professional expertise. "
            "The initial cost is higher, although the lightweight material typically does not "
            "require specialized framing."
        ),
        "expectedLifespan": "40–70 years",
        "availabilityNote": (
            "Special order at The Home Depot Canada; speak with an in-store associate."
        ),
        "specialOrder": True,
        "unit": "panel",
    },
    {
        "id": "default-corrugated-steel-roofing",
        "name": "Corrugated steel roofing",
        "category": "Metal Roofing",
        "description": (
            "Steel roofing combines strength and style. Corrugated steel is a cost-effective and "
            "durable metal roofing option. Metal roofing is lightweight and low maintenance, and "
            "usually does not require specialized framing."
        ),
        "availabilityNote": (
            "Special order at The Home Depot Canada; speak with an in-store associate."
        ),
        "specialOrder": True,
        "unit": "panel",
    },
    {
        "id": "default-stone-coated-steel-roofing",
        "name": "Stone-coated steel roofing",
        "category": "Metal Roofing",
        "description": (
            "Stone-coated steel combines the durability of steel with the appearance of other "
            "roofing materials. Its higher upfront cost may be offset by long-term value. Metal "
            "roofing is lightweight and low maintenance, and usually does not require specialized "
            "framing."
        ),
        "expectedLifespan": "30–50 years",
        "availabilityNote": (
            "Special order at The Home Depot Canada; speak with an in-store associate."
        ),
        "specialOrder": True,
        "unit": "panel",
    },
    {
        "id": "default-aluminum-roofing",
        "name": "Aluminum roofing",
        "category": "Metal Roofing",
        "description": (
            "Aluminum is lightweight and corrosion resistant, making it suitable for areas near "
            "water. It offers high durability and low maintenance. The initial investment may be "
            "higher, while its lightweight construction usually does not require special framing."
        ),
        "expectedLifespan": "30–50 years",
        "availabilityNote": (
            "Special order at The Home Depot Canada; speak with an in-store associate."
        ),
        "specialOrder": True,
        "unit": "panel",
    },
)


def _initialize_default_material_catalog(
    catalog_ref: Any, user_ref: Any, settings: Settings
) -> None:
    """Create the editable starter catalog once, without replacing user edits."""
    marker_ref = user_ref.collection("materialCatalogMetadata").document("roofing-defaults-v1")
    if marker_ref.get().exists:
        return

    for default in DEFAULT_MATERIAL_CATALOG:
        material_ref = catalog_ref.document(str(default["id"]))
        if material_ref.get().exists:
            continue
        value = {
            key: item for key, item in default.items() if key != "id"
        }
        value.update(
            {
                "pricePerUnit": "0.00",
                "currency": settings.default_currency.upper(),
                "coveragePerUnit": None,
                "coverageUnit": None,
                "packageSize": None,
                "packageUnit": None,
                "defaultWastePercentage": str(settings.default_waste_percentage),
                "active": True,
                "placeholderPrice": True,
                "updatedAt": _server_time(),
            }
        )
        try:
            material_ref.create(value)
        except Exception:
            # Concurrent first requests may create the same stable starter row.
            if not material_ref.get().exists:
                raise

    try:
        marker_ref.create({"version": 1, "updatedAt": _server_time()})
    except Exception:
        # Another request may have finished initialization at the same time.
        if not marker_ref.get().exists:
            raise


@app.post("/api/materials/initialize-defaults")
def initialize_default_materials(
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> list[dict[str, Any]]:
    user_ref = db.collection("users").document(user_id(user))
    catalog_ref = user_ref.collection("materialCatalog")
    _initialize_default_material_catalog(catalog_ref, user_ref, settings)
    return [{"id": row.id, **(row.to_dict() or {})} for row in catalog_ref.stream()]


@app.post("/api/materials", status_code=201)
def create_material(
    payload: MaterialInput,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    ref = db.collection("users").document(user_id(user)).collection("materialCatalog").document()
    value = _jsonable(payload.model_dump())
    value["currency"] = (payload.currency or settings.default_currency).upper()
    value["defaultWastePercentage"] = str(
        payload.defaultWastePercentage
        if payload.defaultWastePercentage is not None
        else settings.default_waste_percentage
    )
    value["updatedAt"] = _server_time()
    ref.create(value)
    return {"id": ref.id, **_jsonable(ref.get().to_dict() or {})}


@app.patch("/api/materials/{material_id}")
def patch_material(
    material_id: str,
    payload: MaterialInput,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    ref = (
        db.collection("users")
        .document(user_id(user))
        .collection("materialCatalog")
        .document(material_id)
    )
    if not ref.get().exists:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Material not found."}
        )
    value = _jsonable(payload.model_dump())
    value["currency"] = (payload.currency or settings.default_currency).upper()
    value["defaultWastePercentage"] = str(
        payload.defaultWastePercentage
        if payload.defaultWastePercentage is not None
        else settings.default_waste_percentage
    )
    value["updatedAt"] = _server_time()
    ref.update(value)
    return {"id": material_id, **_jsonable(ref.get().to_dict() or {})}


@app.delete("/api/materials/{material_id}", status_code=204)
def delete_material(
    material_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> None:
    ref = (
        db.collection("users")
        .document(user_id(user))
        .collection("materialCatalog")
        .document(material_id)
    )
    if not ref.get().exists:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Material not found."}
        )
    ref.delete()


@app.post("/api/projects/{project_id}/estimates", status_code=201)
def create_estimate(
    project_id: str,
    payload: EstimateCreate,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
    settings: Settings = Depends(current_settings),
) -> dict[str, Any]:
    project_ref = owned_project(db, user_id(user), project_id)
    if len(payload.lines) > settings.max_estimate_line_items:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_line_items",
                "message": "Estimate has more line items than the configured limit.",
            },
        )
    project = project_ref.get().to_dict() or {}
    measurement_references: list[dict[str, Any]] = []
    waste_recommendations: list[dict[str, Any]] = []
    measurement_version_id = project.get("currentMeasurementVersionId")
    if isinstance(measurement_version_id, str):
        version = (
            project_ref.collection("measurementVersions")
            .document(measurement_version_id)
            .get()
            .to_dict()
            or {}
        )
        raw_references = version.get("materialQuantityReferences", [])
        if isinstance(raw_references, list):
            measurement_references = [item for item in raw_references if isinstance(item, dict)]
        raw_recommendations = version.get("wasteRecommendations", [])
        if isinstance(raw_recommendations, list):
            waste_recommendations = [item for item in raw_recommendations if isinstance(item, dict)]
    trusted_lines = [
        line.model_copy(
            update=_trusted_report_quantity(line, measurement_references, waste_recommendations)
        )
        for line in payload.lines
    ]
    payload = payload.model_copy(update={"lines": trusted_lines})
    result = calculate_estimate(
        payload,
        default_waste_percentage=settings.default_waste_percentage,
        default_markup_percentage=settings.default_markup_percentage,
        default_tax_rate=settings.default_tax_rate,
        default_currency=settings.default_currency,
    )
    estimate_id = f"e_{uuid.uuid4().hex}"
    line_count = len(result["lines"])
    estimate_doc = _jsonable(
        {
            key: result[key]
            for key in (
                "materialSubtotal",
                "labourTotal",
                "disposalTotal",
                "markupPercentage",
                "markupTotal",
                "taxRate",
                "taxTotal",
                "grandTotal",
                "currency",
            )
        }
    )
    estimate_doc.update(
        {
            "additionalChargesTotal": str(result["additionalChargesTotal"]),
            "completeForPricing": result["completeForPricing"],
            "ownerUid": user_id(user),
            "status": "draft",
            "revision": 1,
            "measurementVersionId": project.get("currentMeasurementVersionId"),
            "lineItemCount": line_count,
            "createdAt": _server_time(),
            "updatedAt": _server_time(),
        }
    )
    estimate_ref = project_ref.collection("estimates").document(estimate_id)
    batch = db.batch()
    batch.set(estimate_ref, estimate_doc)
    for index, line in enumerate(result["lines"]):
        batch.set(estimate_ref.collection("lineItems").document(f"{index:03d}"), _jsonable(line))
    batch.commit()
    stored_estimate = _jsonable(estimate_ref.get().to_dict() or {})
    return {"id": estimate_id, **stored_estimate, **_jsonable(result)}


@app.get("/api/projects/{project_id}/estimates")
def list_estimates(
    project_id: str, user: dict[str, Any] = Depends(get_current_user), db: Any = Depends(database)
) -> list[dict[str, Any]]:
    project_ref = owned_project(db, user_id(user), project_id)
    rows = list(project_ref.collection("estimates").stream())
    rows.sort(key=lambda row: _timestamp_order(row.to_dict() or {}), reverse=True)
    return [{"id": row.id, **(row.to_dict() or {})} for row in rows]


@app.get("/api/projects/{project_id}/estimates/{estimate_id}")
def get_estimate(
    project_id: str,
    estimate_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(database),
) -> dict[str, Any]:
    project_ref = owned_project(db, user_id(user), project_id)
    ref = project_ref.collection("estimates").document(estimate_id)
    data = ref.get().to_dict()
    if not data or data.get("ownerUid") != user_id(user):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Estimate not found."}
        )
    rows = list(ref.collection("lineItems").stream())
    rows.sort(key=lambda row: row.id)
    lines = [{"id": row.id, **(row.to_dict() or {})} for row in rows]
    return {"id": estimate_id, **data, "lines": lines}
