import asyncio
import re
from contextlib import suppress
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from starlette.datastructures import Headers

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission
from app.services.drive import DriveStorage
from app.services.ocr import extract_text, suggested_total, suggested_vendor
from app.services.receipt_jobs import enqueue_preview_job, process_preview_job_by_id

router = APIRouter(prefix="/receipts", tags=["receipts"])
settings = get_settings()
ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/webp", "application/pdf",
    "image/heic", "image/heif", "image/jpg", "image/pjpeg", "image/x-png", "application/x-pdf",
}
MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "application/x-pdf": "application/pdf",
    "image/heic": "image/heic",
    "image/heif": "image/heif",
}
CATEGORIES = {"gas", "client_meals", "maintenance", "job_expense", "other"}


def _normalize_upload_mime(content_type: str | None, filename: str | None, content: bytes) -> str:
    ct = (content_type or "").lower().split(";")[0].strip()
    ct = MIME_ALIASES.get(ct, ct)
    if not ct or ct == "application/octet-stream":
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"RIFF") and len(content) > 12 and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"%PDF"):
            return "application/pdf"
        ext = Path(filename or "").suffix.lower()
        ext_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jfif": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp", ".pdf": "application/pdf",
            ".heic": "image/heic", ".heif": "image/heif",
        }
        if ext in ext_map:
            return ext_map[ext]
    return ct


class ReceiptUpdate(BaseModel):
    vendor: str | None = Field(default=None, max_length=160)
    amount: float | None = Field(default=None, ge=0)
    category: str | None = None
    transaction_type: str | None = None
    incurred_at: date | None = None
    link_type: str | None = None
    link_id: str | None = Field(default=None, max_length=160)
    link_label: str | None = Field(default=None, max_length=200)
    vehicle_id: UUID | None = None
    fuel_litres: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)


async def _create_single_preview_job(
    file: UploadFile,
    document_type: str,
    actor_id: UUID,
    db: Any,
    bucket: AsyncIOMotorGridFSBucket,
) -> dict[str, Any]:
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"File '{file.filename}' exceeds upload limit of {settings.max_upload_bytes // (1024*1024)}MB")

    mime_type = _normalize_upload_mime(file.content_type, file.filename, content)
    if mime_type not in ALLOWED_TYPES:
        raise HTTPException(415, f"Unsupported file type for '{file.filename}'. Upload JPEG, PNG, WebP, or PDF.")

    job_id = uuid4()
    filename = _safe_filename(file.filename or "upload")
    now = utcnow()
    expires_at = now + timedelta(hours=1)
    file_id = None
    try:
        file_id = await bucket.upload_from_stream(
            filename, content, metadata={"job_id": str(job_id), "mime_type": mime_type, "expires_at": expires_at},
        )
        await db.ocr_preview_jobs.insert_one({
            "_id": job_id, "status": "queued", "gridfs_file_id": file_id,
            "filename": filename, "mime_type": mime_type, "size_bytes": len(content),
            "document_type": document_type,
            "created_by": actor_id, "created_at": now, "updated_at": now,
            "expires_at": expires_at,
        })
    except Exception:
        if file_id is not None:
            with suppress(Exception):
                await bucket.delete(file_id)
        raise
    await enqueue_preview_job(job_id)
    return {"job_id": str(job_id), "filename": filename, "status": "queued"}


@router.post("/preview", status_code=202)
async def preview_receipt_ocr(
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
    document_type: str = Form("receipt"),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    """Queue OCR preview for single or multiple uploaded files (e.g. mobile multi-select)."""
    if document_type != "receipt":
        raise HTTPException(422, "Document type must be receipt")

    upload_list: list[UploadFile] = []
    if files:
        upload_list.extend(files)
    if file:
        upload_list.append(file)
    if not upload_list:
        raise HTTPException(422, "No upload file provided")

    db = get_database()
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    created_jobs = []
    for uploaded in upload_list:
        job_info = await _create_single_preview_job(uploaded, document_type, actor["_id"], db, bucket)
        created_jobs.append(job_info)

    return {"job_id": created_jobs[0]["job_id"], "status": "queued", "jobs": created_jobs}


@router.post("/preview/batch", status_code=202)
@router.post("/preview-batch", status_code=202)
async def preview_receipt_ocr_batch(
    files: list[UploadFile] = File(...),
    document_type: str = Form("receipt"),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    """Explicit batch endpoint for multi-image mobile uploads."""
    if document_type != "receipt":
        raise HTTPException(422, "Document type must be receipt")
    if not files:
        raise HTTPException(422, "No upload files provided")

    db = get_database()
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    created_jobs = []
    for uploaded in files:
        job_info = await _create_single_preview_job(uploaded, document_type, actor["_id"], db, bucket)
        created_jobs.append(job_info)

    return {"jobs": created_jobs, "count": len(created_jobs)}


@router.get("/preview-jobs")
async def list_receipt_preview_jobs(actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    db = get_database()
    query = {"document_type": "receipt", "created_by": actor["_id"], "expires_at": {"$gt": utcnow()}}

    # Process any queued jobs for this user so multi-image mobile batches update immediately
    queued_jobs = [j["_id"] async for j in db.ocr_preview_jobs.find({**query, "status": "queued"})]
    for q_id in queued_jobs:
        await process_preview_job_by_id(q_id)

    jobs = []
    async for job in db.ocr_preview_jobs.find(query).sort("created_at", -1):
        item = {
            "job_id": str(job["_id"]), "status": job["status"],
            "filename": job["filename"], "documentType": job.get("document_type", "receipt"),
            "mime_type": job["mime_type"], "created_at": job["created_at"],
        }
        if job["status"] == "completed":
            item["preview"] = job.get("result", {})
        elif job["status"] == "failed":
            item["error"] = job.get("error", "OCR preview failed")
        jobs.append(item)
    return jobs



@router.get("/preview/{job_id}")
async def get_receipt_preview_job(
    job_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    db = get_database()
    job = await db.ocr_preview_jobs.find_one({"_id": job_id, "document_type": "receipt", "created_by": actor["_id"]})
    if not job:
        raise HTTPException(404, "Receipt preview job not found or expired")
    if job["status"] == "queued":
        processed = await process_preview_job_by_id(job_id)
        if processed:
            job = processed
    response: dict = {"job_id": str(job_id), "status": job["status"],
                      "filename": job["filename"], "mime_type": job["mime_type"],
                      "documentType": job.get("document_type", "receipt")}
    if job["status"] == "completed":
        response["preview"] = job.get("result", {})
    elif job["status"] == "failed":
        response["error"] = job.get("error", "OCR preview failed")
    return response


@router.get("/preview/{job_id}/file")
async def get_receipt_preview_file(
    job_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    job = await get_database().ocr_preview_jobs.find_one({"_id": job_id, "document_type": "receipt", "created_by": actor["_id"]})
    if not job:
        raise HTTPException(404, "Receipt preview job not found or expired")
    bucket = AsyncIOMotorGridFSBucket(get_database(), bucket_name="ocr_preview_uploads")
    try:
        stream = await bucket.open_download_stream(job["gridfs_file_id"])
        content = await stream.read()
    except Exception as exc:
        raise HTTPException(404, "Temporary receipt file is no longer available") from exc
    return Response(content, media_type=job["mime_type"], headers={
        "Content-Disposition": f'inline; filename="{job["filename"]}"',
        "Cache-Control": "private, no-store",
    })


def _validate(category: str, document_type: str, transaction_type: str) -> None:
    if category not in CATEGORIES:
        raise HTTPException(422, f"Category must be one of: {', '.join(sorted(CATEGORIES))}")
    if document_type != "receipt":
        raise HTTPException(422, "Document type must be receipt")
    if transaction_type not in {"expense", "income"}:
        raise HTTPException(422, "Transaction type must be expense or income")


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:180] or "upload"


async def _validate_job_link(link_type: str | None, link_id: str | None) -> str | None:
    if link_type != "job":
        return link_id
    if not link_id:
        raise HTTPException(422, "Select a job for this document")
    job = await get_database().jobs.find_one({"code": link_id.upper(), "deleted_at": None})
    if job is None:
        try:
            job = await get_database().jobs.find_one({"_id": UUID(link_id), "deleted_at": None})
        except ValueError:
            pass
    if job is None:
        raise HTTPException(422, "Job not found")
    return job["code"]


@router.post("", status_code=201)
async def upload_receipt(
    file: UploadFile = File(...), category: str = Form("other"), document_type: str = Form("receipt"),
    transaction_type: str = Form("expense"), incurred_at: date = Form(default_factory=date.today),
    amount: float | None = Form(None), vendor: str | None = Form(None), currency: str = Form("CAD"),
    link_type: str | None = Form(None), link_id: str | None = Form(None), link_label: str | None = Form(None),
    vehicle_id: UUID | None = Form(None), fuel_litres: float | None = Form(None),
    ocr_text_override: str | None = Form(None),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    _validate(category, document_type, transaction_type)
    if vehicle_id is not None:
        if document_type != "receipt" or category != "gas":
            raise HTTPException(422, "A vehicle can only be assigned to a gas receipt")
        if await get_database().vehicles.find_one({"_id": vehicle_id, "deleted_at": None}) is None:
            raise HTTPException(422, "Vehicle not found")
    if fuel_litres is not None and (vehicle_id is None or category != "gas"):
        raise HTTPException(422, "Fuel litres require a vehicle and gas category")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File is larger than the configured upload limit")
    mime_type = _normalize_upload_mime(file.content_type, file.filename, content)
    if mime_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP, or PDF file")
    filename = _safe_filename(file.filename or "upload")
    ocr_text = ocr_text_override.strip() if ocr_text_override is not None else ""
    if ocr_text_override is None and mime_type.startswith("image/"):
        try:
            ocr_text = await asyncio.to_thread(extract_text, content)
        except Exception:
            ocr_text = ""
    detected_amount = suggested_total(ocr_text)
    detected_vendor = suggested_vendor(ocr_text)
    year = str(incurred_at.year)
    drive_data: dict[str, str] = {}
    storage_provider = "local"
    if settings.google_drive_credentials_json and settings.google_drive_root_folder_id:
        drive = DriveStorage(settings.google_drive_credentials_json, settings.google_drive_root_folder_id)
        folders = ["Receipts", year, category.replace("_", " ").title()]
        drive_data = await asyncio.to_thread(drive.upload, content, filename, file.content_type, folders)
        storage_provider = "google_drive"
    else:
        target = Path(settings.local_upload_directory) / "Receipts" / year / category
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{uuid4()}-{filename}"
        await asyncio.to_thread(path.write_bytes, content)
        drive_data = {"local_path": str(path)}
    now = utcnow()
    cleaned_currency = "CAD" if not currency or currency.upper()[:3] == "CHF" else currency.upper()[:3]
    document = {
        "_id": uuid4(), "document_type": document_type, "transaction_type": transaction_type,
        "category": category, "vendor": vendor or detected_vendor, "amount": amount if amount is not None else detected_amount,
        "currency": cleaned_currency, "incurred_at": datetime.combine(incurred_at, time.min, tzinfo=timezone.utc),
        "filename": filename, "mime_type": file.content_type, "size_bytes": len(content), "ocr_text": ocr_text,
        "link_type": link_type or None, "link_id": link_id or None, "link_label": link_label or None,
        "vehicle_id": vehicle_id, "fuel_litres": fuel_litres,
        "storage_provider": storage_provider, **drive_data, "created_by": actor["_id"], "created_at": now,
        "updated_at": now, "deleted_at": None,
    }
    await get_database().receipts.insert_one(document)
    await write_audit("RECEIPT_UPLOADED", actor["_id"], document_type, document["_id"])
    return serialize(document)


@router.post("/from-preview/{job_id}", status_code=201)
async def upload_receipt_from_preview(
    job_id: UUID, category: str = Form("other"), document_type: str = Form("receipt"),
    transaction_type: str = Form("expense"), incurred_at: date = Form(default_factory=date.today),
    amount: float | None = Form(None), vendor: str | None = Form(None), currency: str = Form("CAD"),
    link_type: str | None = Form(None), link_id: str | None = Form(None), link_label: str | None = Form(None),
    vehicle_id: UUID | None = Form(None), fuel_litres: float | None = Form(None),
    ocr_text_override: str | None = Form(None),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    _validate(category, document_type, transaction_type)
    db = get_database()
    job = await db.ocr_preview_jobs.find_one_and_update(
        {"_id": job_id, "document_type": "receipt", "created_by": actor["_id"], "status": {"$in": ["completed", "failed"]}},
        {"$set": {"status": "confirming", "updated_at": utcnow()}},
        return_document=ReturnDocument.BEFORE,
    )
    if not job:
        raise HTTPException(409, "Receipt preview is not ready, expired, or already being confirmed")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    try:
        stream = await bucket.open_download_stream(job["gridfs_file_id"])
        content = await stream.read()
        reviewed_text = ocr_text_override
        if reviewed_text is None:
            reviewed_text = str(job.get("result", {}).get("ocr_text", ""))
        upload = UploadFile(
            file=BytesIO(content), filename=job["filename"], size=len(content),
            headers=Headers({"content-type": job["mime_type"]}),
        )
        saved = await upload_receipt(
            file=upload, category=category, document_type=document_type,
            transaction_type=transaction_type, incurred_at=incurred_at, amount=amount,
            vendor=vendor, currency=currency, link_type=link_type, link_id=link_id,
            link_label=link_label, vehicle_id=vehicle_id, fuel_litres=fuel_litres,
            ocr_text_override=reviewed_text, actor=actor,
        )
    except Exception:
        await db.ocr_preview_jobs.update_one(
            {"_id": job_id, "status": "confirming"},
            {"$set": {"status": job["status"], "updated_at": utcnow()}},
        )
        raise
    with suppress(Exception):
        await bucket.delete(job["gridfs_file_id"])
    await db.ocr_preview_jobs.delete_one({"_id": job_id})
    return saved


@router.get("")
async def list_receipts(year: int | None = None, category: str | None = None, document_type: str | None = None,
                        _: dict = Depends(require_permission("RECEIPTS_READ"))):
    query: dict = {"deleted_at": None, "document_type": "receipt"}
    if year:
        query["incurred_at"] = {"$gte": datetime(year, 1, 1, tzinfo=timezone.utc), "$lt": datetime(year + 1, 1, 1, tzinfo=timezone.utc)}
    if category:
        query["category"] = category
    if document_type and document_type != "receipt":
        raise HTTPException(422, "Document type must be receipt")
    db = get_database()
    items = []
    async for item in db.receipts.find(query).sort("incurred_at", -1):
        if item.get("currency") in ("CHF", None, ""):
            item["currency"] = "CAD"
        items.append(serialize(item))
    return items


def _resolve_local_file(local_path: str | None) -> Path | None:
    if not local_path:
        return None
    p = Path(local_path)
    if p.is_file():
        return p
    from app.config import BACKEND_ROOT
    candidate = BACKEND_ROOT / local_path
    if candidate.is_file():
        return candidate
    candidate2 = BACKEND_ROOT / settings.local_upload_directory / p.name
    if candidate2.is_file():
        return candidate2
    return None


@router.get("/{receipt_id}/file")
async def download_receipt_file(receipt_id: UUID, _: dict = Depends(require_permission("RECEIPTS_READ"))):
    document = await get_database().receipts.find_one({"_id": receipt_id, "document_type": "receipt", "deleted_at": None})
    if document is None:
        raise HTTPException(404, "Document not found")
    if document.get("storage_provider") == "google_drive" or document.get("web_url"):
        web_url = document.get("web_url")
        if web_url:
            return RedirectResponse(web_url, status_code=307)
    resolved = _resolve_local_file(document.get("local_path"))
    if resolved:
        return FileResponse(resolved, media_type=document.get("mime_type") or "application/octet-stream",
                            filename=document.get("filename") or "receipt",
                            content_disposition_type="inline")
    if document.get("web_url"):
        return RedirectResponse(document["web_url"], status_code=307)
    raise HTTPException(404, "Receipt file is no longer available on disk")




















@router.patch("/{receipt_id}")
async def update_receipt(receipt_id: UUID, payload: ReceiptUpdate, actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    changes = payload.model_dump(exclude_none=True)
    if "link_type" in changes or "link_id" in changes:
        current = await get_database().receipts.find_one({"_id": receipt_id, "document_type": "receipt", "deleted_at": None})
        if not current:
            raise HTTPException(404, "Receipt not found")
        changes["link_id"] = await _validate_job_link(changes.get("link_type", current.get("link_type")),
                                                      changes.get("link_id", current.get("link_id")))
    if "category" in changes and changes["category"] not in CATEGORIES:
        raise HTTPException(422, "Invalid category")
    if "transaction_type" in changes and changes["transaction_type"] not in {"expense", "income"}:
        raise HTTPException(422, "Invalid transaction type")
    if isinstance(changes.get("incurred_at"), date):
        changes["incurred_at"] = datetime.combine(changes["incurred_at"], time.min, tzinfo=timezone.utc)
    changes["updated_at"] = utcnow()
    item = await get_database().receipts.find_one_and_update({"_id": receipt_id, "document_type": "receipt", "deleted_at": None}, {"$set": changes}, return_document=ReturnDocument.AFTER)
    if not item:
        raise HTTPException(404, "Receipt not found")
    await write_audit("RECEIPT_UPDATED", actor["_id"], "receipt", receipt_id, changes)
    return serialize(item)


@router.delete("/{receipt_id}")
async def delete_receipt(receipt_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_DELETE"))):
    result = await get_database().receipts.update_one({"_id": receipt_id, "document_type": "receipt", "deleted_at": None}, {"$set": {"deleted_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "Receipt not found")
    await write_audit("RECEIPT_DELETED", actor["_id"], "receipt", receipt_id)
    return {"success": True}


@router.get("/reports/summary")
async def receipt_summary(year: int | Literal["all"] = date.today().year, job_id: UUID | None = None, currency: str | None = None,
                          _: dict = Depends(require_permission("RECEIPTS_READ"))):
    if year != "all" and (year < 1900 or year > 9998):
        raise HTTPException(422, "Year must be between 1900 and 9998")
    match = {"deleted_at": None, "document_type": "receipt", "amount": {"$ne": None}}
    if year != "all":
        start, end = datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        match["incurred_at"] = {"$gte": start, "$lt": end}
    job = None
    if job_id is not None:
        job = await get_database().jobs.find_one({"_id": job_id, "deleted_at": None})
        if job is None:
            raise HTTPException(404, "Job not found")
        match.update({"link_type": "job", "link_id": {"$in": [job["code"], str(job_id)]}})
    items = [item async for item in get_database().receipts.find(match)]
    income = expenses = 0.0
    categories = {name: 0.0 for name in sorted(CATEGORIES)}
    month_totals = {value: {"income": 0.0, "expenses": 0.0} for value in range(1, 13)}
    week_totals: dict[int, dict[str, float]] = {}
    year_totals: dict[int, dict[str, float]] = {}
    for item in items:
        amount = float(item["amount"])
        bucket = "income" if item.get("transaction_type") == "income" else "expenses"
        if bucket == "income":
            income += amount
        else:
            expenses += amount
            category = item.get("category")
            if category in categories:
                categories[category] += amount
        incurred_at = item["incurred_at"]
        month_totals[incurred_at.month][bucket] += amount
        week = incurred_at.isocalendar().week
        week_totals.setdefault(week, {"income": 0.0, "expenses": 0.0})[bucket] += amount
        year_totals.setdefault(incurred_at.year, {"income": 0.0, "expenses": 0.0})[bucket] += amount
    months = [{"month": value, **month_totals[value]} for value in range(1, 13)]
    weeks = [{"week": value, **week_totals[value]} for value in sorted(week_totals)]
    years = [{"year": value, **year_totals[value]} for value in sorted(year_totals)]
    return {"year": year, "job_id": str(job_id) if job else None,
            "job_code": job["code"] if job else None,
            "currency": "CAD", "currencies": ["CAD"], "mixed_currency": False,
            "income": round(income, 2), "expenses": round(expenses, 2),
            "profit_loss": round(income - expenses, 2), "categories": categories,
            "months": months if year != "all" else [], "weeks": weeks if year != "all" else [], "years": years}
