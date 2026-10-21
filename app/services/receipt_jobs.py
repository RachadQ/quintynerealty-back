import asyncio
import logging
import time
from contextlib import suppress
from datetime import timedelta
from typing import Any
from uuid import UUID

from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pymongo import ReturnDocument

from app.database import get_database
from app.models import utcnow
from app.services.ocr import extract_document, suggested_fields, warm_ocr_engine
from app.services.receipt_items import extract_items

logger = logging.getLogger(__name__)
_worker_task: asyncio.Task[None] | None = None
_wake_worker = asyncio.Event()


async def process_preview_job_by_id(job_id: UUID) -> dict[str, Any] | None:
    """Claim and immediately process a specific queued job."""
    db = get_database()
    job = await db.ocr_preview_jobs.find_one_and_update(
        {"_id": job_id, "status": "queued"},
        {"$set": {"status": "processing", "started_at": utcnow(), "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if job:
        logger.info("[JOB-STEP] Direct claim for queued job %s in active request context", job_id)
        await _process_job(job)
        return await db.ocr_preview_jobs.find_one({"_id": job_id})
    return None


async def enqueue_preview_job(job_id: UUID) -> None:
    _wake_worker.set()


async def cleanup_expired_preview_jobs() -> int:
    """Delete expired job metadata plus GridFS files and their chunk documents."""
    db = get_database()
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    now = utcnow()
    deleted_files = 0
    async for stored_file in bucket.find({"metadata.expires_at": {"$lte": now}}):
        with suppress(Exception):
            await bucket.delete(stored_file._id)
            deleted_files += 1
    result = await db.ocr_preview_jobs.delete_many({"expires_at": {"$lte": now}})
    if deleted_files or result.deleted_count:
        logger.info("Expired receipt previews cleaned", extra={
            "jobs_deleted": result.deleted_count, "files_deleted": deleted_files,
        })
    return int(result.deleted_count)


async def _claim_job() -> dict[str, Any] | None:
    job = await get_database().ocr_preview_jobs.find_one_and_update(
        {"status": "queued"},
        {"$set": {"status": "processing", "started_at": utcnow(), "updated_at": utcnow()}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job:
        logger.info("[JOB-STEP] Claimed queued OCR preview job (job_id=%s, mime=%s)", job["_id"], job.get("mime_type"))
    return job


async def _process_job(job: dict[str, Any]) -> None:
    job_id = str(job["_id"])
    logger.info("[JOB-STEP] Beginning process_job for %s (mime=%s)", job_id, job.get("mime_type"))
    db = get_database()
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    # Keep the temporary upload after OCR so the user can refresh, reopen the
    # preview, and confirm without uploading the receipt a second time.
    delete_upload = False
    try:
        logger.info("[JOB-STEP] Downloading file from GridFS (file_id=%s)", job.get("gridfs_file_id"))
        stream = await bucket.open_download_stream(job["gridfs_file_id"])
        content = await stream.read()
        logger.info("[JOB-STEP] Downloaded %d bytes from GridFS for job %s", len(content), job_id)
        if job["mime_type"].startswith("image/"):
            logger.info("[JOB-STEP] Launching extract_document in background thread for job %s", job_id)
            try:
                extraction = await asyncio.to_thread(extract_document, content)
            except Exception:
                extraction = {"text": "", "engine": "unavailable", "confidence": None}
            text = str(extraction["text"])
            fields = suggested_fields(text)
            logger.info(
                "[JOB-STEP] extract_document returned for job %s: engine=%s, confidence=%s, detected_fields=%s",
                job_id, extraction["engine"], extraction["confidence"], list(fields.keys())
            )
            result = {
                "ocr_text": text,
                **fields,
                "ocr_engine": extraction["engine"],
                "ocr_confidence": extraction["confidence"],
                "message": (
                    "OCR engine is currently unavailable in this environment; please enter receipt details manually."
                    if extraction["engine"] == "unavailable"
                    else None
                ),
            }
        else:
            logger.info("[JOB-STEP] Non-image document (%s) uploaded for job %s; skipping OCR", job["mime_type"], job_id)
            result = {
                "ocr_text": "",
                **suggested_fields(""),
                "ocr_engine": None,
                "ocr_confidence": None,
                "message": "OCR preview currently supports receipt images; PDF files can still be stored.",
            }
        result.update(await extract_items(content, job["mime_type"], result["ocr_text"], result.get("suggested_amount")))
        await db.ocr_preview_jobs.update_one(
            {"_id": job["_id"], "status": "processing"},
            {"$set": {"status": "completed", "result": result, "completed_at": utcnow(),
                      "updated_at": utcnow(), "expires_at": utcnow() + timedelta(hours=1)}},
        )
        logger.info("[JOB-STEP] Job %s successfully marked as completed in MongoDB", job_id)
    except asyncio.CancelledError:
        logger.warning("[JOB-STEP] Job %s was cancelled, resetting to queued status", job_id)
        await db.ocr_preview_jobs.update_one(
            {"_id": job["_id"], "status": "processing"},
            {"$set": {"status": "queued", "updated_at": utcnow()}, "$unset": {"started_at": ""}},
        )
        raise
    except Exception:
        logger.exception("[JOB-STEP] Receipt OCR preview job %s failed with exception", job_id, extra={"job_id": job_id})
        await db.ocr_preview_jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "failed", "error": "OCR could not read this file. Try a clearer photo.",
                      "completed_at": utcnow(), "updated_at": utcnow(),
                      "expires_at": utcnow() + timedelta(hours=1)}},
        )

    finally:
        if delete_upload:
            with suppress(Exception):
                await bucket.delete(job["gridfs_file_id"])


async def _worker() -> None:
    # Recover only stale claims so another live API worker is never interrupted.
    await get_database().ocr_preview_jobs.update_many(
        {"status": "processing", "started_at": {"$lt": utcnow() - timedelta(minutes=30)}},
        {"$set": {"status": "queued", "updated_at": utcnow()}, "$unset": {"started_at": ""}},
    )
    await cleanup_expired_preview_jobs()
    try:
        await asyncio.to_thread(warm_ocr_engine)
    except Exception:
        logger.warning("OCR warm-up failed; the first preview job will retry initialization")
    last_cleanup = time.monotonic()
    while True:
        if time.monotonic() - last_cleanup >= 60:
            await cleanup_expired_preview_jobs()
            last_cleanup = time.monotonic()
        job = await _claim_job()
        if job:
            await _process_job(job)
            continue
        _wake_worker.clear()
        try:
            await asyncio.wait_for(_wake_worker.wait(), timeout=5)
        except TimeoutError:
            pass


def start_preview_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker(), name="receipt-ocr-preview-worker")


async def stop_preview_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with suppress(asyncio.CancelledError):
        await _worker_task
    _worker_task = None
