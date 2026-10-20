"""Firebase Admin initialization and authenticated request dependency."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, cast

from fastapi import Header, HTTPException

from app.config import Settings, get_settings

logger = logging.getLogger("roofing.auth")


def configure_emulator_hosts(settings: Settings) -> None:
    """Export typed settings because Firebase SDKs read emulator hosts from process env."""
    if settings.firestore_emulator_host:
        os.environ["FIRESTORE_EMULATOR_HOST"] = settings.firestore_emulator_host
    if settings.firebase_auth_emulator_host:
        os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = settings.firebase_auth_emulator_host


def _emulator_credential_type() -> Any:
    """Return a credential wrapper for local emulators, which do not require OAuth."""
    from firebase_admin import credentials
    from google.auth.credentials import AnonymousCredentials

    class EmulatorCredential(credentials.Base):  # type: ignore[misc]
        def get_credential(self) -> AnonymousCredentials:
            return AnonymousCredentials()  # type: ignore[no-untyped-call]

    return EmulatorCredential


@lru_cache(maxsize=1)
def get_database() -> Any:
    settings = get_settings()
    configure_emulator_hosts(settings)
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as exc:
        raise RuntimeError("Firebase Admin dependencies are unavailable") from exc
    try:
        app = firebase_admin.get_app()
    except ValueError:
        if settings.google_application_credentials:
            credential = credentials.Certificate(settings.google_application_credentials)
        elif settings.firestore_emulator_host:
            credential = _emulator_credential_type()()
        else:
            credential = credentials.ApplicationDefault()
        app = firebase_admin.initialize_app(credential, {"projectId": settings.firebase_project_id})
    return firestore.client(app=app, database_id=settings.firestore_database_id)


def verify_id_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    configure_emulator_hosts(settings or get_settings())
    try:
        from firebase_admin import auth

        claims = auth.verify_id_token(token, check_revoked=True)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "Sign-in expired. Please sign in again."},
        ) from exc
    if not isinstance(claims.get("uid"), str) or not claims["uid"]:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "Sign-in expired. Please sign in again."},
        )
    return cast(dict[str, Any], claims)


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail={"code": "missing_token", "message": "Sign-in is required."}
        )
    claims = verify_id_token(authorization.removeprefix("Bearer ").strip())
    try:
        from firebase_admin import firestore

        profile = get_database().collection("users").document(claims["uid"])
        if not profile.get().exists:
            try:
                profile.create(
                    {
                        "displayName": claims.get("name", ""),
                        "email": claims.get("email", ""),
                        "createdAt": firestore.SERVER_TIMESTAMP,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    }
                )
            except Exception:
                if not profile.get().exists:
                    raise
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Authenticated profile check failed type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "Firestore is unavailable."},
        ) from exc
    return claims
