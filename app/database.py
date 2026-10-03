from contextlib import suppress
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import OperationFailure

from app.config import get_settings

_client: AsyncIOMotorClient[Any] | None = None
_database: AsyncIOMotorDatabase[Any] | None = None


class DatabaseUnavailableError(RuntimeError):
    """Raised when a request cannot access the required MongoDB database."""


async def connect_database() -> bool:
    global _client, _database
    settings = get_settings()
    _client = AsyncIOMotorClient(
        settings.mongodb_uri,
        uuidRepresentation="standard",
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        maxPoolSize=50,
        minPoolSize=1,
        retryWrites=True,
    )
    try:
        await _client.admin.command("ping")
        # The database name is part of MONGODB_URI (for example, /quintyne_realty_inc).
        # Keeping one source of truth prevents connecting to the right cluster
        # while accidentally selecting the wrong database.
        _database = _client.get_default_database()
        await ensure_indexes(_database)
    except Exception:
        _client.close()
        _client = None
        _database = None
        raise
    return True


async def close_database() -> None:
    global _client, _database
    if _client:
        _client.close()
    _client = None
    _database = None


def get_database() -> AsyncIOMotorDatabase[Any]:
    if _database is None:
        raise DatabaseUnavailableError("MongoDB is unavailable")
    return _database


def database_is_available() -> bool:
    return _database is not None


async def ensure_indexes(db: AsyncIOMotorDatabase[Any]) -> None:
    await db.users.create_indexes([
        IndexModel([("email", ASCENDING)], unique=True),
        IndexModel([("is_active", ASCENDING), ("deleted_at", ASCENDING)]),
    ])
    await db.roles.create_indexes([
        IndexModel([("code", ASCENDING)], unique=True),
        IndexModel([("is_active", ASCENDING)]),
    ])
    await db.audit_logs.create_index([("created_at", DESCENDING)])
    await db.login_activity_logs.create_indexes([
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("created_at", DESCENDING)]),
    ])
    await db.revoked_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.receipts.create_indexes([
        IndexModel([("deleted_at", ASCENDING), ("incurred_at", DESCENDING)]),
        IndexModel([("deleted_at", ASCENDING), ("document_type", ASCENDING), ("category", ASCENDING),
                    ("incurred_at", DESCENDING)]),
        IndexModel([("deleted_at", ASCENDING), ("link_type", ASCENDING), ("link_id", ASCENDING),
                    ("incurred_at", DESCENDING)]),
        IndexModel([("created_by", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("vehicle_id", ASCENDING), ("deleted_at", ASCENDING), ("document_type", ASCENDING),
                    ("category", ASCENDING)]),
        IndexModel([("deleted_at", ASCENDING), ("document_type", ASCENDING), ("incurred_at", DESCENDING),
                    ("currency", ASCENDING)]),
    ])
    await db.jobs.create_indexes([
        IndexModel([("code", ASCENDING)], unique=True),
        IndexModel([("deleted_at", ASCENDING), ("updated_at", DESCENDING)]),
    ])
    await db.job_files.create_indexes([
        IndexModel([("job_id", ASCENDING), ("deleted_at", ASCENDING), ("created_at", DESCENDING)]),
    ])
    await db.quotes.create_indexes([
        IndexModel([("job_id", ASCENDING), ("deleted_at", ASCENDING), ("created_at", DESCENDING)]),
    ])
    await db.rfi_responses.create_indexes([
        IndexModel([("rfi_id", ASCENDING), ("deleted_at", ASCENDING), ("responded_at", ASCENDING)]),
    ])
    await db.rfi_attachments.create_indexes([
        IndexModel([("rfi_id", ASCENDING), ("deleted_at", ASCENDING), ("created_at", DESCENDING)]),
    ])
    await db.rfi_share_links.create_indexes([
        IndexModel([("token_hash", ASCENDING)], unique=True),
        IndexModel([("rfi_id", ASCENDING), ("revoked_at", ASCENDING)]),
    ])
    await db.vehicles.create_indexes([
        IndexModel([("deleted_at", ASCENDING), ("name", ASCENDING)]),
        IndexModel([("deleted_at", ASCENDING), ("is_default", ASCENDING)]),
    ])
    await db.vehicle_trips.create_index([("vehicle_id", ASCENDING), ("deleted_at", ASCENDING), ("occurred_at", DESCENDING)])
    await db.vehicle_trips.create_index([("deleted_at", ASCENDING), ("start_location", ASCENDING), ("end_location", ASCENDING)])
    # Cleanup must remove both GridFS files and chunks, so application code owns
    # expiration instead of MongoDB's document-only TTL deletion.
    preview_indexes = await db.ocr_preview_jobs.index_information()
    if "expireAfterSeconds" in preview_indexes.get("expires_at_1", {}):
        with suppress(OperationFailure):
            await db.ocr_preview_jobs.drop_index("expires_at_1")
    await db.ocr_preview_jobs.create_indexes([
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)]),
        IndexModel([("created_by", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("expires_at", ASCENDING)]),
    ])
    with suppress(Exception):
        await db.receipts.update_many({"currency": "CHF"}, {"$set": {"currency": "CAD"}})
        await db.jobs.update_many({"budget_currency": "CHF"}, {"$set": {"budget_currency": "CAD"}})
        rfi_ids_with_responses = await db.rfi_responses.distinct("rfi_id", {"deleted_at": None})
        if rfi_ids_with_responses:
            await db.receipts.update_many(
                {"_id": {"$in": rfi_ids_with_responses}, "document_type": "rfi"},
                {"$set": {"status": "closed"}}
            )


