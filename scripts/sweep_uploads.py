"""Clean up report uploads left in uploading state beyond their configured TTL."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api"))

from app.config import get_settings
from app.firebase import get_database
from app.services.file_store import delete_file_chunks


def main() -> int:
    logging.basicConfig(level=get_settings().log_level.upper())
    db = get_database()
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.upload_stale_after_seconds)
    cleaned = 0
    for project in db.collection("projects").stream():
        project_data = project.to_dict() or {}
        if not project_data.get("ownerUid"):
            continue
        for report in project.reference.collection("reports").where("status", "==", "uploading").stream():
            data = report.to_dict() or {}
            updated = data.get("updatedAt")
            if not updated:
                continue
            if hasattr(updated, "to_datetime"):
                updated = updated.to_datetime()
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated >= cutoff:
                continue
            report.reference.update({"status": "deleting"})
            delete_file_chunks(db, report.reference, batch_size=settings.firestore_delete_batch_size, retry_attempts=settings.firebase_retry_attempts)
            report.reference.update({"status": "failed", "extractionError": "Incomplete upload was removed by cleanup."})
            cleaned += 1
    logging.info("Cleaned stale report uploads count=%s", cleaned)
    return cleaned


if __name__ == "__main__":
    main()
