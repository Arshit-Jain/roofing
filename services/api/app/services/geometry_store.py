"""Keep compact geometry inline and route larger immutable payloads through chunks."""

from __future__ import annotations

import hashlib
import io
import json
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.services.file_store import store_file_chunks
from app.services.firestore_values import firestore_safe_value


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError("Geometry contains an unsupported value")


def store_geometry_version(
    db: Any,
    project_ref: Any,
    geometry_version_ref: Any,
    geometry: dict[str, Any],
    *,
    owner_uid: str,
    settings: Settings,
) -> dict[str, Any]:
    """Hide Firestore's document-size tradeoff from geometry callers."""
    payload = json.dumps(
        geometry, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    base: dict[str, Any] = {"geometrySha256": digest, "geometrySizeBytes": len(payload)}
    if len(payload) <= settings.geometry_inline_limit_bytes:
        base.update(
            {"geometry": firestore_safe_value(geometry), "geometryArtifactId": None}
        )
        geometry_version_ref.update(base)
        return base
    if len(payload) > settings.max_artifact_bytes:
        raise ValueError("Geometry artifact exceeds configured size limit")
    artifact_id = f"geometry_{geometry_version_ref.id}_{digest[:12]}"
    artifact_ref = project_ref.collection("artifacts").document(artifact_id)
    artifact_ref.create(
        {
            "kind": "geometry_json",
            "ownerUid": owner_uid,
            "contentType": "application/json",
            "fileSizeBytes": len(payload),
            "sha256": digest,
            "chunkSizeBytes": settings.file_chunk_size_bytes,
            "chunkCount": 0,
            "status": "uploading",
            "createdAt": _server_timestamp(),
        }
    )
    metadata = store_file_chunks(
        db,
        artifact_ref,
        io.BytesIO(payload),
        chunk_size_bytes=settings.file_chunk_size_bytes,
        batch_size=settings.firestore_chunk_batch_size,
        max_file_size_bytes=settings.max_artifact_bytes,
        retry_attempts=settings.firebase_retry_attempts,
    )
    artifact_ref.update(
        {**metadata.as_document_fields(), "status": "complete", "updatedAt": _server_timestamp()}
    )
    base.update({"geometry": None, "geometryArtifactId": artifact_id})
    geometry_version_ref.update(base)
    return base


def _server_timestamp() -> Any:
    from firebase_admin import firestore

    return firestore.SERVER_TIMESTAMP
