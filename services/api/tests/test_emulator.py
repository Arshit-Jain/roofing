from __future__ import annotations

import json
import os
import socket
import uuid

import pytest
from fastapi.encoders import jsonable_encoder

from app.config import Settings
from app.firebase import configure_emulator_hosts, get_database
from app.main import create_project
from app.schemas import ProjectCreate


@pytest.mark.emulator
def test_firestore_emulator_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host:
        pytest.skip(
            "FIRESTORE_EMULATOR_HOST is not set; start the Firestore emulator to run this test"
        )
    if not host.startswith(("localhost:", "127.0.0.1:", "[::1]:")):
        pytest.skip("Refusing emulator test because FIRESTORE_EMULATOR_HOST is not loopback")
    address = host.removeprefix("[").replace("]", "").rsplit(":", 1)
    try:
        with socket.create_connection((address[0], int(address[1])), timeout=1):
            pass
    except (OSError, ValueError, IndexError):
        pytest.skip("Firestore emulator is not listening at FIRESTORE_EMULATOR_HOST")

    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-roofing-local")
    settings = Settings(
        _env_file=None,
        firebase_project_id="demo-roofing-local",
        firestore_emulator_host=host,
    )
    configure_emulator_hosts(settings)
    from app.config import get_settings

    get_settings.cache_clear()
    client = get_database()
    reference = client.collection("_emulatorTests").document(uuid.uuid4().hex)
    reference.set({"status": "round_trip"})
    project_response: dict[str, object] | None = None
    try:
        assert reference.get().to_dict() == {"status": "round_trip"}
        project_response = create_project(
            ProjectCreate(name="Emulator serialization check"),
            {"uid": "emulator-test-user"},
            client,
        )
        serialized = json.loads(json.dumps(jsonable_encoder(project_response)))
        assert serialized["name"] == "Emulator serialization check"
        assert isinstance(serialized["createdAt"], str)
    finally:
        reference.delete()
        if project_response is not None:
            client.collection("projects").document(str(project_response["id"])).delete()
        get_settings.cache_clear()
