from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.audit import write_audit
from app.database import get_database
from app.models import RoleCreate, RoleUpdate, UserCreate, UserUpdate, serialize, user_document, utcnow
from app.security import get_current_user, require_permission, user_permissions

router = APIRouter(tags=["rbac"])


@router.get("/me/permissions")
async def my_permissions(user: dict = Depends(get_current_user)):
    return {"permissions": sorted(await user_permissions(user))}


@router.get("/configuration/users")
async def list_users(is_active: bool | None = None, _: dict = Depends(require_permission("CONFIG_USERS_READ"))):
    query = {"deleted_at": None}
    if is_active is not None:
        query["is_active"] = is_active
    return [serialize(item) async for item in get_database().users.find(query).sort("full_name", 1)]


@router.post("/configuration/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, actor: dict = Depends(require_permission("CONFIG_USERS_CREATE"))):
    document = user_document(payload, actor["_id"])
    try:
        await get_database().users.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="A user with this email already exists") from exc
    await write_audit("USER_CREATED", actor["_id"], "user", document["_id"])
    return serialize(document)


@router.patch("/configuration/users/{user_id}")
async def update_user(user_id: UUID, payload: UserUpdate, actor: dict = Depends(require_permission("CONFIG_USERS_UPDATE"))):
    changes = payload.model_dump(exclude_none=True)
    changes["updated_at"] = utcnow()
    user = await get_database().users.find_one_and_update({"_id": user_id, "deleted_at": None},
        {"$set": changes}, return_document=ReturnDocument.AFTER)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await write_audit("USER_UPDATED", actor["_id"], "user", user_id, changes)
    return serialize(user)


@router.delete("/configuration/users/{user_id}")
async def delete_user(user_id: UUID, actor: dict = Depends(require_permission("CONFIG_USERS_DELETE"))):
    if user_id == actor["_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    result = await get_database().users.update_one({"_id": user_id, "deleted_at": None},
        {"$set": {"deleted_at": utcnow(), "is_active": False, "updated_at": utcnow()}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    await write_audit("USER_DELETED", actor["_id"], "user", user_id)
    return {"success": True}


@router.get("/configuration/roles")
async def list_roles(_: dict = Depends(require_permission("CONFIG_ROLES_READ"))):
    return [serialize(item) async for item in get_database().roles.find({}).sort("name", 1)]


@router.post("/configuration/roles", status_code=status.HTTP_201_CREATED)
async def create_role(payload: RoleCreate, actor: dict = Depends(require_permission("CONFIG_ROLES_CREATE"))):
    document = {"_id": uuid4(), **payload.model_dump(), "is_active": True, "is_system_role": False,
                "created_at": utcnow(), "updated_at": utcnow(), "created_by": actor["_id"]}
    try:
        await get_database().roles.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Role code already exists") from exc
    await write_audit("ROLE_CREATED", actor["_id"], "role", document["_id"])
    return serialize(document)


@router.patch("/configuration/roles/{role_id}")
async def update_role(role_id: UUID, payload: RoleUpdate, actor: dict = Depends(require_permission("CONFIG_ROLES_UPDATE"))):
    changes = payload.model_dump(exclude_none=True)
    changes["updated_at"] = utcnow()
    role = await get_database().roles.find_one_and_update({"_id": role_id}, {"$set": changes},
        return_document=ReturnDocument.AFTER)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    await write_audit("ROLE_UPDATED", actor["_id"], "role", role_id, changes)
    return serialize(role)


@router.get("/audit-logs")
async def audit_logs(limit: int = 100, _: dict = Depends(require_permission("AUDIT_LOG_READ"))):
    limit = max(1, min(limit, 500))
    return [serialize(item) async for item in get_database().audit_logs.find({"action": {"$nin": ["LOGIN", "LOGOUT"]}}).sort("created_at", -1).limit(limit)]

