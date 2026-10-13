from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "production"] = "production"
    service_name: str = "quintyne-realty-inc-backend"
    mongodb_uri: str
    jwt_secret: str = Field(min_length=32)
    session_secret: str = Field(min_length=32)
    jwt_issuer: str = "quintyne-realty-inc-backend"
    jwt_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    frontend_url: str
    cors_allowed_origins: str
    trusted_hosts: str
    auth_cookie_secure: bool = True
    session_cookie_secure: bool = True
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    dev_auth_bypass: bool = False
    dev_auth_email: str = "developer@localhost"
    dev_auth_name: str = "Local Developer"
    allowed_email_domains: str = ""
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    ocr_engine: Literal["paddle", "rapid"] = "paddle"
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_redirect_uri: str = ""
    microsoft_tenant_id: str = "common"
    google_drive_credentials_json: str = ""
    google_drive_root_folder_id: str = ""
    local_upload_directory: str = "uploads"

    @field_validator("mongodb_uri")
    @classmethod
    def validate_mongodb_uri(cls, value: str) -> str:
        if not value.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must be a MongoDB connection URI")
        return value

    @field_validator("frontend_url")
    @classmethod
    def validate_frontend_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        parsed = urlparse(clean)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("FRONTEND_URL must be an absolute HTTP(S) origin without credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("FRONTEND_URL must not contain a path, query, or fragment")
        return clean

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        for item in (part.strip() for part in value.split(",")):
            if not item:
                continue
            parsed = urlparse(item)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Every CORS origin must be an absolute HTTP(S) origin")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("CORS origins must not contain paths, queries, or fragments")
        return value

    @property
    def origins(self) -> list[str]:
        return [item.strip().rstrip("/") for item in self.cors_allowed_origins.split(",") if item.strip()]

    @property
    def hosts(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def email_domains(self) -> set[str]:
        return {item.strip().lower().lstrip("@") for item in self.allowed_email_domains.split(",") if item.strip()}

    def validate_production(self) -> None:
        if self.app_env == "development":
            loopback_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
            if urlparse(self.frontend_url).hostname not in loopback_hosts:
                raise RuntimeError("Development FRONTEND_URL must use a loopback host")
            if any(urlparse(origin).hostname not in loopback_hosts for origin in self.origins):
                raise RuntimeError("Development CORS origins must use loopback hosts")
            if any(host not in loopback_hosts for host in self.hosts):
                raise RuntimeError("Development TRUSTED_HOSTS must contain only loopback hosts")
            return
        if self.jwt_secret == self.session_secret:
            raise RuntimeError("JWT_SECRET and SESSION_SECRET must be different in production")
        if not self.auth_cookie_secure or not self.session_cookie_secure:
            raise RuntimeError("Secure cookies are required in production")
        if not self.origins or "*" in self.origins:
            raise RuntimeError("Explicit CORS origins are required in production")
        if urlparse(self.frontend_url).scheme != "https":
            raise RuntimeError("FRONTEND_URL must use HTTPS in production")
        if any(urlparse(origin).scheme != "https" for origin in self.origins):
            raise RuntimeError("Every production CORS origin must use HTTPS")
        if "*" in self.hosts or not self.hosts:
            raise RuntimeError("Production TRUSTED_HOSTS must contain explicit hostnames")
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise RuntimeError("SameSite=None requires secure authentication cookies")


@lru_cache
def get_settings() -> Settings:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        reasons = []
        for error in exc.errors(include_input=False, include_context=False):
            field = str(error["loc"][0]).upper()
            kind = error["type"]
            if kind == "missing":
                reason = "not supplied to this deployment or empty"
            elif kind == "string_too_short":
                reason = "must contain at least 32 characters" if field in {
                    "JWT_SECRET", "SESSION_SECRET"
                } else "value is too short"
            elif field in {"FRONTEND_URL", "CORS_ALLOWED_ORIGINS"}:
                reason = "expected HTTP(S) origin with no credentials, path, query, or fragment"
                if field == "CORS_ALLOWED_ORIGINS":
                    reason += "; separate multiple origins with commas"
            elif kind == "bool_parsing":
                reason = "expected true or false"
            else:
                reason = "invalid value (" + kind + ")"
            reasons.append(field + ": " + reason)
        raise RuntimeError(
            "Deployment configuration failed: " + "; ".join(sorted(reasons))
            + ". Update this deployment's environment variables and redeploy."
        ) from None
    settings.validate_production()
    return settings
