"""Small Firestore-shaped fake for deterministic unit and route tests."""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


class FakeServerTimestamp:
    pass


def _resolve_server_timestamps(value: Any) -> Any:
    if isinstance(value, FakeServerTimestamp):
        return datetime(2026, 1, 1, tzinfo=UTC)
    if isinstance(value, dict):
        return {key: _resolve_server_timestamps(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_server_timestamps(item) for item in value]
    return deepcopy(value)


class Snapshot:
    def __init__(self, reference: DocRef, data: dict[str, Any] | None):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = deepcopy(data)

    def to_dict(self) -> dict[str, Any] | None:
        return deepcopy(self._data)


class DocRef:
    def __init__(self, client: FakeFirestore, path: tuple[str, ...]):
        self.client = client
        self.path = path
        self.id = path[-1]

    def collection(self, name: str) -> Collection:
        return Collection(self.client, self.path + (name,))

    def get(self, transaction: Any = None) -> Snapshot:
        return Snapshot(self, self.client.rows.get(self.path))

    def create(self, data: dict[str, Any]) -> None:
        with self.client.lock:
            if self.path in self.client.rows:
                raise ValueError("already exists")
            self.client.rows[self.path] = _resolve_server_timestamps(data)

    def set(self, data: dict[str, Any]) -> None:
        self.client.rows[self.path] = _resolve_server_timestamps(data)

    def update(self, data: dict[str, Any]) -> None:
        if self.path not in self.client.rows:
            raise ValueError("missing document")
        self.client.rows[self.path].update(_resolve_server_timestamps(data))

    def delete(self) -> None:
        self.client.rows.pop(self.path, None)


class Collection:
    def __init__(self, client: FakeFirestore, path: tuple[str, ...]):
        self.client = client
        self.path = path

    def document(self, document_id: str | None = None) -> DocRef:
        return DocRef(self.client, self.path + (document_id or f"auto{len(self.client.rows)}",))

    def stream(self) -> list[Snapshot]:
        return [
            Snapshot(DocRef(self.client, path), data)
            for path, data in self.client.rows.items()
            if len(path) == len(self.path) + 1 and path[:-1] == self.path
        ]

    def where(self, field: str, operator: str, value: Any) -> Collection:
        return self

    def limit(self, amount: int) -> Collection:
        return self

    def get(self) -> list[Snapshot]:
        return self.stream()


class Batch:
    def __init__(self, client: FakeFirestore):
        self.client = client
        self.operations: list[tuple[str, DocRef, dict[str, Any] | None]] = []

    def set(self, ref: DocRef, data: dict[str, Any]) -> None:
        self.operations.append(("set", ref, deepcopy(data)))

    def delete(self, ref: DocRef) -> None:
        self.operations.append(("delete", ref, None))

    def commit(self) -> None:
        self.client.batch_calls += 1
        if self.client.fail_next_batch:
            self.client.fail_next_batch = False
            raise RuntimeError("injected failure")
        if self.client.fail_on_batch_number == self.client.batch_calls:
            raise RuntimeError("injected mid-upload failure")
        for action, ref, data in self.operations:
            if action == "set":
                ref.set(data or {})
            else:
                ref.delete()


class FakeFirestore:
    def __init__(self):
        self.rows: dict[tuple[str, ...], dict[str, Any]] = {}
        self.fail_next_batch = False
        self.fail_on_batch_number: int | None = None
        self.batch_calls = 0
        self.lock = threading.RLock()

    def collection(self, name: str) -> Collection:
        return Collection(self, (name,))

    def batch(self) -> Batch:
        return Batch(self)

    def transaction(self, *, max_attempts: int = 5) -> Transaction:
        del max_attempts
        return Transaction(self)


class Transaction:
    def __init__(self, client: FakeFirestore):
        self.client = client
        self.operations: list[tuple[str, DocRef, dict[str, Any] | None]] = []
        self._id: bytes | None = None
        self._max_attempts = 5
        self._read_only = False

    @property
    def in_progress(self) -> bool:
        return self._id is not None

    def _clean_up(self) -> None:
        self._id = None
        self.operations.clear()

    def _begin(self, retry_id: bytes | None = None) -> None:
        del retry_id
        self._id = b"fake-transaction"

    def _commit(self) -> None:
        self.commit()

    def _rollback(self) -> None:
        self._clean_up()

    def __enter__(self) -> Transaction:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, ref: DocRef) -> Snapshot:
        return ref.get()

    def set(self, ref: DocRef, data: dict[str, Any]) -> None:
        self.operations.append(("set", ref, data))

    def update(self, ref: DocRef, data: dict[str, Any]) -> None:
        self.operations.append(("update", ref, data))

    def commit(self) -> None:
        for action, ref, data in self.operations:
            if action == "set":
                ref.set(data or {})
            elif action == "update":
                ref.update(data or {})
        self._clean_up()
