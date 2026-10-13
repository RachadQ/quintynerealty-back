import hashlib
import hmac
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status

from app.config import get_settings
from app.database import get_database

AUTH_COOKIE_NAME = "access_token"
CSRF_COOKIE_NAME = "csrf_token"
JWT_ALGORITHM = "HS256"


def create_access_token(user_id: UUID, is_super_admin: bool) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.jwt_ttl_minutes)
    token = jwt.encode({
        "sub": str(user_id), "admin": is_super_admin, "iat": now,
        "exp": expires_at, "iss": settings.jwt_issuer, "jti": secrets.token_urlsafe(24),
    }, settings.jwt_secret, algorithm=JWT_ALGORITHM)
    return token, expires_at


def set_auth_cookies(response: Response, token: str) -> None:
    settings = get_settings()
    csrf = secrets.token_urlsafe(32)
    common = dict(secure=settings.auth_cookie_secure, samesite=settings.auth_cookie_samesite, path="/")
    response.set_cookie(AUTH_COOKIE_NAME, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE_NAME, csrf, httponly=False, **common)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie(alias=AUTH_COOKIE_NAME)] = None,
) -> dict:
    settings = get_settings()
    if settings.dev_auth_bypass:
        return {
            "_id": UUID("00000000-0000-4000-8000-000000000001"),
            "email": settings.dev_auth_email.lower().strip(),
            "full_name": settings.dev_auth_name.strip(),
            "is_active": True, "is_super_admin": True, "role_ids": [],
            "auth_provider": "localhost_development", "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc), "deleted_at": None,
            "token_payload": {"jti": "localhost-development", "exp": 0},
        }
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else access_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM], issuer=settings.jwt_issuer,
                             options={"require": ["sub", "exp", "iat", "iss", "jti"]})
        user_id = UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired session") from exc
    db = get_database()
    if await db.revoked_tokens.find_one({"_id": payload["jti"]}):
        raise HTTPException(status_code=401, detail="Session has been revoked")
    user = await db.users.find_one({"_id": user_id, "is_active": True, "deleted_at": None})
    if not user:
        raise HTTPException(status_code=401, detail="User is inactive or no longer exists")
    user["token_payload"] = payload
    return user


async def user_permissions(user: dict) -> set[str]:
    if user.get("is_super_admin"):
        return {"*"}
    role_ids = user.get("role_ids", [])
    cursor = get_database().roles.find({"_id": {"$in": role_ids}, "is_active": True}, {"permissions": 1})
    permissions: set[str] = set()
    async for role in cursor:
        permissions.update(role.get("permissions", []))
    return permissions


def require_permission(permission: str) -> Callable:
    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        permissions = await user_permissions(user)
        if "*" not in permissions and permission not in permissions:
            raise HTTPException(status_code=403, detail="Insufficient permission")
        return user
    return dependency


async def csrf_protect(request: Request) -> None:
    settings = get_settings()
    if settings.dev_auth_bypass:
        return
    if request.method in {"GET", "HEAD", "OPTIONS"} or request.headers.get("authorization", "").startswith("Bearer "):
        return
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


class RateLimiter:
    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, request: Request) -> None:
        import time
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = self.requests[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": str(self.window_seconds)})
        bucket.append(now)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]
