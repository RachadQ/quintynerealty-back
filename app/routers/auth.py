from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import serialize, utcnow
from app.security import (CSRF_COOKIE_NAME, clear_auth_cookies, create_access_token,
                          get_current_user, set_auth_cookies, user_permissions)

router = APIRouter(prefix="/auth", tags=["authentication"])
settings = get_settings()


class GoogleLoginRequest(BaseModel):
    credential: str = Field(min_length=1, max_length=8192)


@router.get("/google/config")
async def google_config():
    return {"client_id": settings.google_oauth_client_id}


@router.post("/google")
async def google_login(payload: GoogleLoginRequest, request: Request, response: Response):
    if not settings.google_oauth_client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    if request.headers.get("origin") != settings.frontend_url:
        raise HTTPException(status_code=403, detail="Sign-in request origin is not allowed")

    try:
        claims = await run_in_threadpool(
            id_token.verify_oauth2_token,
            payload.credential,
            GoogleRequest(),
            settings.google_oauth_client_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Google credential") from exc
    email = claims.get("email", "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Google account email is not verified")

    db = get_database()
    user = await db.users.find_one({"email": email, "is_active": True, "deleted_at": None})
    if not user:
        raise HTTPException(status_code=403, detail="This Google account is not enabled in the administration portal")

    now = utcnow()
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": now}})
    token, _ = create_access_token(user["_id"], bool(user.get("is_super_admin")))
    csrf = set_auth_cookies(response, token)
    await write_audit("LOGIN", user["_id"], "session", metadata={"provider": "google"})
    await db.login_activity_logs.insert_one({
        "user_id": user["_id"], "provider": "google", "created_at": now,
    })
    clean = {key: value for key, value in user.items() if key != "token_payload"}
    return {"success": True, "user": serialize(clean), "permissions": sorted(await user_permissions(user)), "csrf_token": csrf}


@router.get("/bootstrap")
async def bootstrap(request: Request, user: dict = Depends(get_current_user)):
    permissions = await user_permissions(user)
    clean = {k: v for k, v in user.items() if k != "token_payload"}
    return {"user": serialize(clean), "permissions": sorted(permissions),
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            "dev_auth_bypass": settings.dev_auth_bypass}


@router.get("/csrf")
async def csrf_token(request: Request, response: Response, user: dict = Depends(get_current_user)):
    if settings.dev_auth_bypass:
        return {"csrf_token": "", "dev_auth_bypass": True}
    csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf:
        token = request.cookies.get("access_token")
        if not token:
            raise HTTPException(status_code=401, detail="Cookie session required")
        csrf = set_auth_cookies(response, token)
    return {"csrf_token": csrf}


@router.post("/heartbeat")
async def heartbeat(request: Request, response: Response, user: dict = Depends(get_current_user)):
    if settings.dev_auth_bypass:
        return {"status": "ok", "mode": "localhost_development", "dev_auth_bypass": True}
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"last_activity_at": utcnow()}})
    token, _ = create_access_token(user["_id"], bool(user.get("is_super_admin")))
    csrf = set_auth_cookies(response, token, request.cookies.get(CSRF_COOKIE_NAME))
    return {"status": "ok", "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)):
    if settings.dev_auth_bypass:
        response = JSONResponse({"status": "success"})
        clear_auth_cookies(response)
        request.session.clear()
        return response
    response = JSONResponse({"status": "success"})
    clear_auth_cookies(response)
    request.session.clear()
    payload = user["token_payload"]
    await get_database().revoked_tokens.update_one({"_id": payload["jti"]},
        {"$set": {"expires_at": datetime.fromtimestamp(payload["exp"], tz=timezone.utc)}}, upsert=True)
    await write_audit("LOGOUT", user["_id"], "session")
    return response
