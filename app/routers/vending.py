"""Saved vending machine locations, using the shared Canadian address lookup."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument

from app.audit import write_audit
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission
from app.routers.vehicles import _client, _geocode, place_suggestions

router = APIRouter(prefix="/vending-machines", tags=["vending-machines"])


class MachineInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=5, max_length=200)
    notes: str = Field(default="", max_length=1000)


@router.get("")
async def list_machines(_: dict = Depends(require_permission("RECEIPTS_READ"))):
    return [serialize(item) async for item in get_database().vending_machines.find(
        {"deleted_at": None}).sort("name", 1)]


@router.get("/locations")
async def lookup_address(q: str = Query(min_length=3, max_length=200),
                         actor: dict = Depends(require_permission("RECEIPTS_READ"))):
    return await place_suggestions(q, actor)


async def location_fields(payload: MachineInput) -> dict:
    longitude, latitude, label = await _geocode(_client(), payload.address)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise HTTPException(502, "Address lookup returned an invalid location. Try a more complete address.")
    return {**payload.model_dump(), "latitude": latitude, "longitude": longitude,
            "resolved_address": label, "updated_at": utcnow()}


@router.post("", status_code=201)
async def create_machine(payload: MachineInput, actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    fields = await location_fields(payload)
    document = {"_id": uuid4(), **fields, "created_at": utcnow(),
                "created_by": actor["_id"], "deleted_at": None}
    await get_database().vending_machines.insert_one(document)
    await write_audit("VENDING_MACHINE_CREATED", actor["_id"], "vending_machine", document["_id"])
    return serialize(document)


@router.put("/{machine_id}")
async def update_machine(machine_id: UUID, payload: MachineInput,
                         actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    collection = get_database().vending_machines
    if await collection.find_one({"_id": machine_id, "deleted_at": None}) is None:
        raise HTTPException(404, "Vending machine not found")
    fields = await location_fields(payload)
    document = await collection.find_one_and_update(
        {"_id": machine_id, "deleted_at": None}, {"$set": fields}, return_document=ReturnDocument.AFTER)
    if document is None:
        raise HTTPException(404, "Vending machine not found")
    await write_audit("VENDING_MACHINE_UPDATED", actor["_id"], "vending_machine", machine_id)
    return serialize(document)


@router.delete("/{machine_id}", status_code=204)
async def delete_machine(machine_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_DELETE"))):
    result = await get_database().vending_machines.update_one(
        {"_id": machine_id, "deleted_at": None}, {"$set": {"deleted_at": utcnow(), "updated_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "Vending machine not found")
    await write_audit("VENDING_MACHINE_DELETED", actor["_id"], "vending_machine", machine_id)
