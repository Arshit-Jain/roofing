from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_local_configuration_has_bounded_safe_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.file_chunk_size_bytes == 524_288
    assert settings.firestore_chunk_batch_size == 8
    assert settings.default_currency == "CAD"
    assert settings.job_poll_interval_seconds == 2
    assert settings.job_poll_timeout_seconds == 180


def test_cors_origins_are_split_and_normalized() -> None:
    settings = Settings(
        _env_file=None, cors_allowed_origins="https://roof.example, http://localhost:3000/"
    )
    assert settings.cors_allowed_origins == ["https://roof.example", "http://localhost:3000"]


def test_configuration_rejects_unsafe_chunk_batch_and_cors_values() -> None:
    with pytest.raises(ValidationError, match="must not exceed 9 MiB"):
        Settings(_env_file=None, file_chunk_size_bytes=700_000, firestore_chunk_batch_size=14)
    with pytest.raises(ValidationError, match=r"absolute HTTP\(S\) origins"):
        Settings(_env_file=None, cors_allowed_origins="*")


def test_production_requires_explicit_non_local_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    with pytest.raises(ValidationError, match="explicit production project ID"):
        Settings(_env_file=None, app_env="production")
    with pytest.raises(ValidationError, match="localhost"):
        Settings(
            _env_file=None,
            app_env="production",
            firebase_project_id="roofline-prod",
            api_host="0.0.0.0",
            cors_allowed_origins=["https://localhost:3000"],
        )
