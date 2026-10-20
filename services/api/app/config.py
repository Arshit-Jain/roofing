"""Validated environment configuration for the API."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = "development"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    api_host: Annotated[str, Field(min_length=1, max_length=253)] = "127.0.0.1"
    api_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    request_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 60
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    firebase_project_id: str = "demo-roofing-local"
    firestore_database_id: str = "(default)"
    google_application_credentials: str | None = None
    firestore_emulator_host: str | None = None
    firebase_auth_emulator_host: str | None = None

    nvidia_api_key: str | None = Field(default=None, validation_alias="NVIDIA_API_KEY", repr=False)
    nvidia_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    nvidia_api_url: str = "https://integrate.api.nvidia.com/v1/chat/completions"
    nvidia_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120
    nvidia_max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    nvidia_reasoning_budget: Annotated[int, Field(ge=0, le=65_536)] = 16_384
    nvidia_max_output_tokens: Annotated[int, Field(ge=256, le=65_536)] = 65_536
    extraction_schema_version: str = "2"
    extraction_prompt_version: str = "3"
    malformed_output_retries: Annotated[int, Field(ge=0, le=2)] = 1

    max_upload_bytes: Annotated[int, Field(ge=1, le=100 * 1024 * 1024)] = 20 * 1024 * 1024
    max_pdf_pages: Annotated[int, Field(ge=1, le=500)] = 100
    max_estimate_line_items: Annotated[int, Field(ge=1, le=499)] = 100
    max_material_selections: Annotated[int, Field(ge=1, le=100)] = 30
    file_chunk_size_bytes: Annotated[int, Field(ge=1, le=716_800)] = 524_288
    firestore_chunk_batch_size: Annotated[int, Field(ge=1, le=32)] = 8
    firestore_delete_batch_size: Annotated[int, Field(ge=1, le=450)] = 400
    upload_stale_after_seconds: Annotated[int, Field(ge=60, le=86_400)] = 900
    job_stale_after_seconds: Annotated[int, Field(ge=60, le=86_400)] = 1800
    job_poll_interval_seconds: Annotated[float, Field(ge=0.5, le=30)] = 2
    job_poll_timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 180
    max_artifact_bytes: Annotated[int, Field(ge=1, le=100 * 1024 * 1024)] = 20 * 1024 * 1024
    geometry_inline_limit_bytes: Annotated[int, Field(ge=1024, le=700 * 1024)] = 400 * 1024
    upload_read_size_bytes: Annotated[int, Field(ge=1024, le=1024 * 1024)] = 64 * 1024
    firebase_retry_attempts: Annotated[int, Field(ge=0, le=5)] = 3
    job_max_concurrency: Annotated[int, Field(ge=1, le=16)] = 2

    geometry_pitch_tolerance_degrees: Annotated[Decimal, Field(ge=0, le=45)] = Decimal("1")
    geometry_snap_tolerance_points: Annotated[float, Field(gt=0, le=10)] = 0.8
    geometry_scale_tolerance_percent: Annotated[float, Field(ge=0, le=25)] = 3.0
    geometry_facet_area_tolerance_percent: Annotated[float, Field(ge=0, le=25)] = 5.0
    geometry_edge_class_tolerance_percent: Annotated[float, Field(ge=0, le=25)] = 3.0
    geometry_height_tolerance_feet: Annotated[float, Field(gt=0, le=1)] = 0.05
    geometry_wall_depth_feet: Annotated[float, Field(gt=0, le=100)] = 8.0
    geometry_page_render_dpi: Annotated[int, Field(ge=72, le=300)] = 144

    default_currency: str = "CAD"
    default_tax_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0")
    default_waste_percentage: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("8")
    default_markup_percentage: Annotated[Decimal, Field(ge=0, le=1000)] = Decimal("0")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return [
                origin.strip().rstrip("/")
                for origin in value
                if isinstance(origin, str) and origin.strip()
            ]
        return value

    @field_validator("app_env")
    @classmethod
    def valid_app_env(cls, value: str) -> str:
        if value not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENV must be development, test, staging, or production")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def valid_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("default_currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        if len(value) != 3 or not value.isalpha():
            raise ValueError("DEFAULT_CURRENCY must be a three-letter currency code")
        return value.upper()

    @model_validator(mode="after")
    def production_settings_are_explicit(self) -> Settings:
        for origin in self.cors_allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
            ):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must contain absolute HTTP(S) origins without paths"
                )
        if not self.cors_allowed_origins or "*" in self.cors_allowed_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must list one or more explicit origins")
        if self.file_chunk_size_bytes * self.firestore_chunk_batch_size > 9 * 1024 * 1024:
            raise ValueError(
                "FILE_CHUNK_SIZE_BYTES times FIRESTORE_CHUNK_BATCH_SIZE must not exceed 9 MiB"
            )
        if self.app_env == "production":
            if self.firebase_project_id.startswith("demo-"):
                raise ValueError("FIREBASE_PROJECT_ID must be an explicit production project ID")
            if self.firestore_emulator_host or self.firebase_auth_emulator_host:
                raise ValueError("Firebase emulator hosts cannot be set in production")
            if "*" in self.cors_allowed_origins:
                raise ValueError("CORS_ALLOWED_ORIGINS cannot contain a wildcard in production")
            if any(not origin.startswith("https://") for origin in self.cors_allowed_origins):
                raise ValueError("Production CORS origins must use HTTPS")
            if any(
                "localhost" in origin or "127.0.0.1" in origin
                for origin in self.cors_allowed_origins
            ):
                raise ValueError("Production CORS origins must not point to localhost")
            if self.api_host in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError(
                    "Production API_HOST must be explicitly configured for the deployment"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
