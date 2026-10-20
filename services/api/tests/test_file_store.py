from __future__ import annotations

import hashlib
import io

import pytest

from app.services.file_store import (
    delete_file_chunks,
    read_file_chunks,
    store_file_chunks,
    verify_file_integrity,
)
from tests.fakes import FakeFirestore


def test_single_chunk_round_trip_and_sha256() -> None:
    db = FakeFirestore()
    parent = db.collection("projects").document("p").collection("reports").document("r")
    payload = b"%PDF-1.7 sample pdf payload"
    metadata = store_file_chunks(
        db, parent, io.BytesIO(payload), chunk_size_bytes=64, max_file_size_bytes=100
    )
    fields = metadata.as_document_fields()
    fields["chunkCount"] = metadata.chunk_count
    target = io.BytesIO()
    read_file_chunks(parent, target, fields)
    assert target.getvalue() == payload
    assert metadata.sha256 == hashlib.sha256(payload).hexdigest()
    assert verify_file_integrity(io.BytesIO(target.getvalue()), metadata)


def test_multi_chunk_read_uses_numeric_index_not_query_order() -> None:
    db = FakeFirestore()
    parent = db.collection("artifacts").document("a")
    payload = bytes(range(251)) * 3
    metadata = store_file_chunks(
        db, parent, io.BytesIO(payload), chunk_size_bytes=100, batch_size=2
    )
    fields = metadata.as_document_fields() | {"chunkCount": metadata.chunk_count}
    collection = parent.collection("chunks")
    rows = list(collection.stream())
    shuffled = [(row.id, row.to_dict() or {}) for row in reversed(rows)]
    for row in rows:
        collection.document(row.id).delete()
    for document_id, data in shuffled:
        collection.document(document_id).set(data)
    target = io.BytesIO()
    read_file_chunks(parent, target, fields)
    assert target.getvalue() == payload
    assert metadata.chunk_count == 8


def test_missing_chunk_checksum_or_file_hash_is_rejected() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    metadata = store_file_chunks(db, parent, io.BytesIO(b"x" * 20), chunk_size_bytes=10)
    fields = metadata.as_document_fields() | {"chunkCount": metadata.chunk_count}
    parent.collection("chunks").document("000000").update({"data": b"bad"})
    with pytest.raises(ValueError, match="checksum"):
        read_file_chunks(parent, io.BytesIO(), fields)
    original_chunk = b"x" * 10
    parent.collection("chunks").document("000000").update(
        {"data": original_chunk, "checksum": hashlib.sha256(original_chunk).hexdigest()}
    )
    fields["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        read_file_chunks(parent, io.BytesIO(), fields)


def test_empty_oversized_and_unsafe_chunk_are_rejected() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    with pytest.raises(ValueError, match="Empty"):
        store_file_chunks(db, parent, io.BytesIO(b""))
    with pytest.raises(ValueError, match="size limit"):
        store_file_chunks(
            db, parent, io.BytesIO(b"123456"), chunk_size_bytes=3, max_file_size_bytes=5
        )
    with pytest.raises(ValueError, match="safe maximum"):
        store_file_chunks(db, parent, io.BytesIO(b"x"), chunk_size_bytes=716_801)


def test_failed_batch_cleans_partial_chunks() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    db.fail_next_batch = True
    with pytest.raises(RuntimeError, match="injected"):
        store_file_chunks(db, parent, io.BytesIO(b"x" * 20), chunk_size_bytes=5, batch_size=1)
    assert parent.collection("chunks").stream() == []


def test_failure_after_a_committed_batch_cleans_all_partial_chunks() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    db.fail_on_batch_number = 2
    with pytest.raises(RuntimeError, match="mid-upload"):
        store_file_chunks(db, parent, io.BytesIO(b"x" * 20), chunk_size_bytes=5, batch_size=1)
    assert parent.collection("chunks").stream() == []


def test_deterministic_chunk_ids_make_retries_idempotent() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    payload = b"same file contents"
    first = store_file_chunks(db, parent, io.BytesIO(payload), chunk_size_bytes=5)
    second = store_file_chunks(db, parent, io.BytesIO(payload), chunk_size_bytes=5)
    assert first == second
    assert len(parent.collection("chunks").stream()) == first.chunk_count


def test_delete_removes_every_chunk() -> None:
    db = FakeFirestore()
    parent = db.collection("reports").document("r")
    store_file_chunks(db, parent, io.BytesIO(b"x" * 30), chunk_size_bytes=10)
    assert delete_file_chunks(db, parent) == 3
    assert parent.collection("chunks").stream() == []
