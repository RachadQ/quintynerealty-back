import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.database import (DatabaseUnavailableError, close_database, connect_database,
                          database_is_available, get_database)
from app.routers.auth import router as auth_router
from app.routers.onedrive import router as onedrive_router
from app.routers.rbac import router as rbac_router
from app.routers.receipts import router as receipts_router
from app.routers.vehicles import close_external_client, router as vehicles_router
from app.security import RateLimiter, csrf_protect
from app.services.receipt_jobs import start_preview_worker, stop_preview_worker

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(settings.service_name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database_connected = False
    try:
        await connect_database()
        database_connected = True
        logger.info("MongoDB connection established")
        start_preview_worker()
    except Exception as exc:
        if settings.app_env != "development":
            raise
        logger.warning("MongoDB unavailable in development (%s); database routes will return 503", type(exc).__name__)
    try:
        yield
    finally:
        if database_connected:
            await stop_preview_worker()
        await close_external_client()
        await close_database()


app = FastAPI(title="Quintyne Realty inc API", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.hosts)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.session_cookie_secure,
                   same_site=settings.auth_cookie_samesite)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"])

rate_limiter = RateLimiter()


@app.exception_handler(DatabaseUnavailableError)
async def database_unavailable_handler(_: Request, exc: DatabaseUnavailableError):
    return JSONResponse(status_code=503, content={
        "detail": str(exc),
        "code": "DATABASE_UNAVAILABLE",
    })


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or secrets.token_urlsafe(12)
    started = time.monotonic()
    try:
        await rate_limiter(request)
        await csrf_protect(request)
        response = await call_next(request)
    except Exception:
        raise
    headers = {
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'", "X-Request-ID": request_id,
        "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-site",
    }
    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers.update(headers)
    logger.info("request", extra={"method": request.method, "path": request.url.path,
                                  "status": response.status_code, "duration_ms": round((time.monotonic()-started)*1000, 2)})
    return response


app.include_router(auth_router)
app.include_router(rbac_router)
app.include_router(receipts_router)
app.include_router(vehicles_router)
app.include_router(onedrive_router)


@app.get("/")
async def root():
    return {"service": settings.service_name, "status": "ok"}


@app.get("/health")
async def health():
    if not database_is_available():
        return JSONResponse(status_code=503, content={
            "status": "degraded", "database": "unavailable",
        })
    await get_database().command("ping")
    return {"status": "healthy", "database": "connected"}
