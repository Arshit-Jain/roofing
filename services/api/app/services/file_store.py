"""Bounded, integrity-checked file chunks stored in Firestore subcollections."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol, TypeVar

T = TypeVar("T")


class Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class DocumentReference(Protocol):
    def collection(self, name: str) -> CollectionReference: ...
    def get(self) -> Snapshot: ...


class CollectionReference(Protocol):
    def document(self, document_id: str) -> DocumentReference: ...
    def stream(self) -> list[Snapshot]: ...


class Batch(Protocol):
    def set(self, ref: DocumentReference, data: dict[str, object]) -> None: ...
    def delete(self, ref: DocumentReference) -> None: ...
    def commit(self) -> object: ...


class BatchClient(Protocol):
    def batch(self) -> Batch: ...


@dataclass(frozen=True)
class ChunkMetadata:
    file_size_bytes: int
    sha256: str
    chunk_size_bytes: int
    chunk_count: int

    def as_document_fields(self) -> dict[str, object]:
        return {
            "fileSizeBytes": self.file_size_bytes,
            "sha256": self.sha256,
            "chunkSizeBytes": self.chunk_size_bytes,
            "chunkCount": self.chunk_count,
        }


def _retry(operation: Callable[[], T], attempts: int) -> T:
    """Bound retry wrapper; Firestore errors are transient only when the SDK says so."""
    from google.api_core import exceptions as api_errors

    transient = (
        api_errors.Aborted,
        api_errors.DeadlineExceeded,
        api_errors.InternalServerError,
        api_errors.ServiceUnavailable,
        api_errors.ResourceExhausted,
    )
    for index in range(attempts + 1):
        try:
            return operation()
        except transient:
            if index >= attempts:
                raise
            time.sleep(min(0.15 * (2**index), 1.2))
    raise RuntimeError("unreachable retry state")


def store_file_chunks(
    client: BatchClient,
    parent_ref: DocumentReference,
    file_obj: BinaryIO,
    *,
    chunk_size_bytes: int = 524_288,
    batch_size: int = 8,
    max_file_size_bytes: int = 20 * 1024 * 1024,
    retry_attempts: int = 3,
) -> ChunkMetadata:
    """Stream a file into deterministic chunk documents without reading it all at once."""
    if chunk_size_bytes > 716_800:
        raise ValueError("Configured Firestore chunk size exceeds the safe maximum")
    if chunk_size_bytes < 1 or batch_size < 1 or batch_size > 32:
        raise ValueError("Invalid chunk or batch size")
    if chunk_size_bytes * batch_size > 9 * 1024 * 1024:
        raise ValueError("Configured Firestore batch could exceed the safe request size")
    collection = parent_ref.collection("chunks")
    whole_hash = hashlib.sha256()
    size = 0
    index = 0
    pending: list[tuple[DocumentReference, dict[str, object]]] = []

    def flush() -> None:
        if not pending:
            return
        batch = client.batch()
        for ref, value in pending:
            batch.set(ref, value)
        _retry(batch.commit, retry_attempts)
        pending.clear()

    try:
        while True:
            payload = file_obj.read(chunk_size_bytes)
            if not payload:
                break
            size += len(payload)
            if size > max_file_size_bytes:
                raise ValueError("File exceeds configured storage size limit")
            whole_hash.update(payload)
            chunk_ref = collection.document(f"{index:06d}")
            pending.append(
                (
                    chunk_ref,
                    {
                        "index": index,
                        "data": payload,
                        "checksum": hashlib.sha256(payload).hexdigest(),
                    },
                )
            )
            index += 1
            if len(pending) >= batch_size:
                flush()
        flush()
    except Exception:
        delete_file_chunks(client, parent_ref, retry_attempts=retry_attempts)
        raise
    if size == 0:
        raise ValueError("Empty files cannot be stored")
    return ChunkMetadata(size, whole_hash.hexdigest(), chunk_size_bytes, index)


def verify_file_integrity(file_obj: BinaryIO, metadata: ChunkMetadata | dict[str, object]) -> bool:
    """Verify a completed file stream against stored size and SHA-256 metadata."""
    expected_size = (
        metadata.file_size_bytes
        if isinstance(metadata, ChunkMetadata)
        else metadata.get("fileSizeBytes")
    )
    expected_sha = (
        metadata.sha256 if isinstance(metadata, ChunkMetadata) else metadata.get("sha256")
    )
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or not isinstance(expected_sha, str)
    ):
        return False
    digest = hashlib.sha256()
    size = 0
    while True:
        block = file_obj.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
        size += len(block)
    return size == expected_size and digest.hexdigest() == expected_sha


def read_file_chunks(
    parent_ref: DocumentReference, output: BinaryIO, metadata: dict[str, object]
) -> None:
    """Reassemble numeric chunk order and verify contiguity, checksums, size, and hash."""
    count = metadata.get("chunkCount")
    expected_size = metadata.get("fileSizeBytes")
    expected_sha = metadata.get("sha256")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or not isinstance(expected_sha, str)
    ):
        raise ValueError("Stored file metadata is invalid")
    if count < 1 or expected_size < 1:
        raise ValueError("Stored file metadata is invalid")
    verify_chunk_count(parent_ref, count)
    chunks = parent_ref.collection("chunks")
    whole_hash = hashlib.sha256()
    total_size = 0
    for index in range(count):
        snapshot = chunks.document(f"{index:06d}").get()
        row = snapshot.to_dict() if snapshot.exists else None
        if row is None or row.get("index") != index:
            raise ValueError("Stored file chunks are incomplete")
        payload = row.get("data")
        checksum = row.get("checksum")
        if not isinstance(payload, bytes) or not isinstance(checksum, str):
            raise ValueError("Stored file chunk is invalid")
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise ValueError("Stored file chunk checksum did not match")
        output.write(payload)
        whole_hash.update(payload)
        total_size += len(payload)
    if total_size != expected_size or whole_hash.hexdigest() != expected_sha:
        raise ValueError("Stored file integrity check failed")
    output.flush()


def verify_chunk_count(parent_ref: DocumentReference, expected_count: int) -> None:
    """Verify chunk indices using a payload-free field projection."""
    collection = parent_ref.collection("chunks")
    select = getattr(collection, "select", None)
    snapshots = list(select(["index"]).stream() if select else collection.stream())
    indices: list[int] = []
    for snapshot in snapshots:
        row = snapshot.to_dict() or {}
        index = row.get("index")
        if not isinstance(index, int):
            raise ValueError("Stored file chunk metadata is invalid")
        indices.append(index)
    if sorted(indices) != list(range(expected_count)):
        raise ValueError("Stored file chunk count or indices are invalid")


def delete_file_chunks(
    client: BatchClient,
    parent_ref: DocumentReference,
    *,
    batch_size: int = 400,
    retry_attempts: int = 3,
) -> int:
    """Delete all child chunks in bounded batches, leaving the parent to its caller."""
    if batch_size < 1 or batch_size > 450:
        raise ValueError("Delete batch size must be between 1 and 450")
    refs: list[DocumentReference] = []
    collection = parent_ref.collection("chunks")
    select = getattr(collection, "select", None)
    snapshots = select(["index"]).stream() if select else collection.stream()
    for snapshot in snapshots:
        ref = getattr(snapshot, "reference", None)
        if ref is not None:
            refs.append(ref)
    for start in range(0, len(refs), batch_size):
        batch = client.batch()
        for ref in refs[start : start + batch_size]:
            batch.delete(ref)
        _retry(batch.commit, retry_attempts)
    verify_chunk_count(parent_ref, 0)
    return len(refs)
