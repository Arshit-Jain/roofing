from __future__ import annotations

import os
import sys
from types import ModuleType

import pytest
from fastapi import HTTPException

from app import firebase


def install_auth_module(monkeypatch: pytest.MonkeyPatch, verify_token) -> None:
    firebase_admin = ModuleType("firebase_admin")
    auth_module = ModuleType("firebase_admin.auth")
    auth_module.verify_id_token = verify_token  # type: ignore[attr-defined]
    firebase_admin.auth = auth_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "firebase_admin", firebase_admin)
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", auth_module)


def test_valid_id_token_returns_verified_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    def verify(token: str, *, check_revoked: bool) -> dict[str, str]:
        assert token == "valid-token"
        assert check_revoked is True
        return {"uid": "contractor-1", "email": "person@example.test"}

    install_auth_module(monkeypatch, verify)
    claims = firebase.verify_id_token("valid-token", Settings(_env_file=None))
    assert claims["uid"] == "contractor-1"


def test_emulator_hosts_loaded_from_typed_settings_reach_firebase_sdks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Settings

    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    firebase.configure_emulator_hosts(
        Settings(
            _env_file=None,
            firestore_emulator_host="127.0.0.1:8080",
            firebase_auth_emulator_host="127.0.0.1:9099",
        )
    )
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    assert os.environ["FIRESTORE_EMULATOR_HOST"] == "127.0.0.1:8080"
    assert os.environ["FIREBASE_AUTH_EMULATOR_HOST"] == "127.0.0.1:9099"


def test_firestore_emulator_uses_anonymous_credentials() -> None:
    from google.auth.credentials import AnonymousCredentials

    from app.config import Settings

    settings = Settings(_env_file=None, firestore_emulator_host="127.0.0.1:8080")
    firebase.configure_emulator_hosts(settings)
    credential_type = firebase._emulator_credential_type()
    credential = credential_type().get_credential()

    assert isinstance(credential, AnonymousCredentials)


@pytest.mark.parametrize("reason", ["invalid token", "expired token"])
def test_invalid_or_expired_id_token_returns_sanitized_401(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from app.config import Settings

    def reject(_: str, *, check_revoked: bool) -> dict[str, str]:
        del check_revoked
        raise ValueError(reason)

    install_auth_module(monkeypatch, reject)
    with pytest.raises(HTTPException) as caught:
        firebase.verify_id_token("bad-token", Settings(_env_file=None))
    assert caught.value.status_code == 401
    assert "bad-token" not in str(caught.value.detail)
    assert reason not in str(caught.value.detail)
