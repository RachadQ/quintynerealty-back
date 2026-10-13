"""Vehicle fuel balances calculated from gas receipts and logged trips."""

import asyncio
import csv
import hashlib
import json
import tempfile
import time as clock
from pathlib import Path
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from uuid import UUID, uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import httpx

from app.audit import write_audit
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission
from app.services.trip_spreadsheet import sync_safely

router = APIRouter(prefix="/vehicles", tags=["vehicles"])
CATALOG_URLS = {
    "2026": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/9df1b18d-d036-4783-a61c-99f1f75b3ac5/download/my2026-fuel-consumption-ratings.csv",
    "2025": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/d589f2bc-9a85-4f65-be2f-20f17debfcb1/download/my2025-fuel-consumption-ratings.csv",
    "2015-2024": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/c98b9dc8-b23f-4cd8-8b19-e892da1e4688/download/my2015-2024-fuel-consumption-ratings.csv",
    "1995-2014": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/42495676-28b7-40f3-b0e0-3d7fe005ca56/download/my1995-2014-fuel-consumption-ratings-5-cycle.csv",
}
_catalog_cache: dict[str, list[dict]] = {}
_catalog_locks: dict[str, asyncio.Lock] = {}
CATALOG_CACHE_DIRECTORY = Path(tempfile.gettempdir()) / "quintyne-vehicle-catalog"
CATALOG_CACHE_TTL = 86400
_place_cache: dict[str, list[dict]] = {}
_coordinate_cache: dict[str, tuple[float, float, str]] = {}
_external_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _external_client
    if _external_client is None or _external_client.is_closed:
        _external_client = httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": "QuintyneRealtyIncAdministration/1.0"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _external_client


async def close_external_client() -> None:
    global _external_client
    if _external_client is not None:
        await _external_client.aclose()
        _external_client = None


def _street_tokens(value: str) -> set[str]:
    """Return normalized street-name tokens without city, province, or postal code."""
    value = re.sub(r"\b[a-z]\d[a-z]\s*\d[a-z]\d\b", " ", value.casefold())
    tokens = re.findall(r"[a-z0-9]+", value)
    if tokens and tokens[0].isdigit():
        tokens = tokens[1:]
    aliases = {"tenth": "10th", "tneth": "10th", "street": "st", "road": "rd",
               "avenue": "ave", "boulevard": "blvd", "drive": "dr"}
    suffixes = {"st", "rd", "ave", "blvd", "dr", "line", "lane", "way", "court", "crescent"}
    directions = {"east", "west", "north", "south", "e", "w", "n", "s"}
    street = []
    found_suffix = False
    for token in tokens:
        token = aliases.get(token, token)
        if found_suffix and token not in directions:
            break
        street.append(token)
        if token in suffixes:
            found_suffix = True
    return set(street)


class VehicleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    make: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=80)
    year: int | None = Field(default=None, ge=1981, le=2100)
    fuel_efficiency_l_per_100km: float = Field(gt=0, le=100, allow_inf_nan=False)
    monthly_insurance_cost: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    annual_total_km: float | None = Field(default=None, gt=0, le=1000000, allow_inf_nan=False)


class InsuranceUpdate(BaseModel):
    monthly_insurance_cost: float = Field(ge=0, le=100000, allow_inf_nan=False)


class TripCreate(BaseModel):
    purpose: Literal["business", "personal"] = "business"
    receipt_id: UUID | None = None
    distance_km: float = Field(gt=0, le=100000, allow_inf_nan=False)
    occurred_at: date = Field(default_factory=date.today)
    notes: str = Field(default="", max_length=500)
    start_location: str = Field(default="", max_length=300)
    end_location: str = Field(default="", max_length=300)


class RouteRequest(BaseModel):
    start_location: str = Field(min_length=3, max_length=300)
    end_location: str = Field(min_length=3, max_length=300)


def fuel_summary(vehicle: dict, receipts: list[dict], trips: list[dict]) -> dict:
    efficiency = Decimal(str(vehicle["fuel_efficiency_l_per_100km"]))
    purchased = sum((Decimal(str(item.get("fuel_litres") or 0)) for item in receipts), Decimal("0"))
    distance = sum((Decimal(str(item["distance_km"])) for item in trips), Decimal("0"))
    used = distance * efficiency / Decimal("100")
    remaining = purchased - used
    available = max(remaining, Decimal("0"))
    quantize = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"purchased_litres": quantize(purchased), "trip_distance_km": quantize(distance),
            "used_litres": quantize(used), "remaining_litres": quantize(remaining),
            "estimated_range_km": quantize(available * Decimal("100") / efficiency),
            "overdrawn": remaining < 0, "gas_receipt_count": len(receipts), "trip_count": len(trips)}


def receipt_usage(vehicle: dict, receipts: list[dict], trips: list[dict]) -> list[dict]:
    """Allocate logged fuel FIFO by receipt date, splitting trips across receipts."""
    efficiency = Decimal(str(vehicle["fuel_efficiency_l_per_100km"]))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    def order(item, field):
        value = item.get(field) or item.get("created_at") or oldest
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        created = item.get("created_at") or oldest
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (value, created, str(item["_id"]))
    rows = [{"receipt_id": str(item["_id"]), "purchased": Decimal(str(item.get("fuel_litres") or 0)),
             "used": Decimal("0"), "distance": Decimal("0"), "trip_count": 0}
            for item in sorted((r for r in receipts if not r.get("deleted_at")), key=lambda r: order(r, "incurred_at"))]
    cursor = 0
    for trip in sorted((t for t in trips if not t.get("deleted_at")), key=lambda t: order(t, "occurred_at")):
        needed = Decimal(str(trip["distance_km"])) * efficiency / Decimal("100")
        while needed > 0 and cursor < len(rows):
            row = rows[cursor]
            available = row["purchased"] - row["used"]
            if available <= 0:
                cursor += 1
                continue
            consumed = min(needed, available)
            row["used"] += consumed
            row["distance"] += consumed * Decimal("100") / efficiency
            row["trip_count"] += 1
            needed -= consumed
    rounded = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return [{"receipt_id": row["receipt_id"], "trip_count": row["trip_count"],
             "distance_km": rounded(row["distance"]), "used_litres": rounded(row["used"]),
             "remaining_litres": rounded(max(row["purchased"] - row["used"], Decimal("0")))} for row in rows]


def insurance_summary(vehicle: dict, trips: list[dict], year: int) -> dict:
    monthly = Decimal(str(vehicle.get("monthly_insurance_cost") or 0))
    yearly_trips = [item for item in trips if not item.get("deleted_at")
                    and item.get("occurred_at") and item["occurred_at"].year == year]
    total_km = sum((Decimal(str(item["distance_km"])) for item in yearly_trips), Decimal("0"))
    business_km = sum((Decimal(str(item["distance_km"])) for item in yearly_trips
                       if item.get("purpose", "business") == "business"), Decimal("0"))
    annual_cost = monthly * Decimal("12")
    ratio = min(business_km / total_km, Decimal("1")) if total_km > 0 else Decimal("0")
    money = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"year": year, "monthly_cost": money(monthly), "annual_cost": money(annual_cost),
            "annual_total_km": money(total_km), "business_km": money(business_km),
            "personal_km": money(total_km - business_km), "trip_count": len(yearly_trips),
            "business_use_percent": money(ratio * Decimal("100")),
            "deductible_amount": money(annual_cost * ratio), "currency": "CAD"}


def _read_catalog_cache(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if clock.time() - data["fetched_at"] < CATALOG_CACHE_TTL:
            rows = data["records"]
            if isinstance(rows, list) and all(isinstance(row, dict) and {"year", "make", "model", "combined"} <= row.keys() for row in rows):
                return rows
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _parse_catalog(content: bytes) -> list[dict]:
    rows = csv.DictReader(StringIO(content.decode("utf-8-sig", errors="replace").replace("\r\r\n", "\n")))
    return [{"year": int(row["Model year"]), "make": row["Make"].strip(),
             "model": row["Model"].strip(), "combined": float(row["Combined (L/100 km)"])}
            for row in rows if row.get("Model year") and row.get("Combined (L/100 km)")]


def _write_catalog_cache(path: Path, records: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"fetched_at": clock.time(), "records": records}), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass  # An unwritable cache must not prevent vehicle selection.


async def _catalog(year: int) -> list[dict]:
    key = str(year) if year >= 2025 else "2015-2024" if year >= 2015 else "1995-2014"
    async with _catalog_locks.setdefault(key, asyncio.Lock()):
        if key not in _catalog_cache:
            path = CATALOG_CACHE_DIRECTORY / (hashlib.sha256(CATALOG_URLS[key].encode()).hexdigest() + ".json")
            records = await asyncio.to_thread(_read_catalog_cache, path)
            if records is None:
                try:
                    response = await _client().get(CATALOG_URLS[key], timeout=30, follow_redirects=True)
                    response.raise_for_status()
                    records = await asyncio.to_thread(_parse_catalog, response.content)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    raise HTTPException(502, "Canadian vehicle ratings are temporarily unavailable. Try again or enter the vehicle manually.") from exc
                await asyncio.to_thread(_write_catalog_cache, path, records)
            _catalog_cache[key] = records
    return [item for item in _catalog_cache[key] if item["year"] == year]


@router.get("/catalog")
async def vehicle_catalog(year: int, make: str | None = None,
                          _: dict = Depends(require_permission("RECEIPTS_READ"))):
    if year < 1995 or year > 2026:
        raise HTTPException(422, "Canadian ratings are available for model years 1995 through 2026")
    records = await _catalog(year)
    if not make:
        makes = sorted({item["make"] for item in records})
        models_by_make = {}
        for item in records:
            models_by_make.setdefault(item["make"], {})[(item["model"], item["combined"])] = {
                "model": item["model"], "fuel_efficiency_l_per_100km": item["combined"]}
        return {"year": year, "makes": makes, "models_by_make": {
            name: sorted(options.values(), key=lambda item: item["model"]) for name, options in models_by_make.items()}}
    options = [{"model": item["model"], "fuel_efficiency_l_per_100km": item["combined"]}
               for item in records if item["make"].casefold() == make.casefold()]
    unique = {(item["model"], item["fuel_efficiency_l_per_100km"]): item for item in options}
    return {"year": year, "make": make, "vehicles": sorted(unique.values(), key=lambda item: item["model"])}


@router.get("/place-suggestions")
async def place_suggestions(q: str, _: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    query = q.strip()
    if len(query) < 3 or len(query) > 200:
        return []
    cache_key = query.casefold()
    if cache_key in _place_cache:
        return _place_cache[cache_key]
    street_number = query.split(maxsplit=1)[0] if query[:1].isdigit() else ""
    provider_query = re.sub(r"\btneth\b", "Tenth", query, flags=re.IGNORECASE)
    async def lookup(provider: str) -> list[dict]:
        try:
            if provider == "canada":
                response = await _client().get("https://geolocator.api.geo.ca/",
                    params={"q": provider_query, "lang": "en", "keys": "locate,fsa"},
                    timeout=httpx.Timeout(1.8, connect=1.0))
                response.raise_for_status()
                return [{"label": str(item["name"]), "longitude": item["lng"], "latitude": item["lat"],
                         "provider": "Natural Resources Canada"} for item in response.json()[:8]
                        if item.get("name") and item.get("lat") is not None and item.get("lng") is not None]
            response = await _client().get("https://photon.komoot.io/api/",
                params={"q": provider_query, "limit": 6, "lang": "en", "bbox": "-141,41,-52,84"},
                timeout=httpx.Timeout(1.8, connect=1.0))
            response.raise_for_status()
            results = []
            for feature in response.json().get("features", []):
                props = feature.get("properties", {})
                if props.get("countrycode") and props["countrycode"].casefold() != "ca":
                    continue
                if street_number and str(props.get("housenumber") or "") != street_number:
                    continue
                coordinates = feature.get("geometry", {}).get("coordinates", [])
                parts = [f'{props.get("housenumber", "")} {props.get("street", "")}'.strip(),
                         props.get("city") or props.get("district"), props.get("state")]
                label = ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))
                if label and len(coordinates) == 2:
                    results.append({"label": label, "longitude": coordinates[0], "latitude": coordinates[1], "provider": "Photon"})
            return results
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return []
    tasks = [asyncio.create_task(lookup(provider)) for provider in ("canada", "photon")]
    suggestions = []
    try:
        for future in asyncio.as_completed(tasks, timeout=1.8):
            results = await future
            exact = [item for item in results if not street_number or
                     (item["label"].startswith(street_number + " ") and
                      _street_tokens(provider_query) <= _street_tokens(item["label"]))]
            if exact:
                suggestions = exact
                break
            suggestions.extend(results)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    suggestions = list({item["label"]: item for item in suggestions}.values())[:8]
    if suggestions:
        if len(_place_cache) >= 250:
            _place_cache.pop(next(iter(_place_cache)))
        _place_cache[cache_key] = suggestions
        for item in suggestions:
            if len(_coordinate_cache) >= 500:
                _coordinate_cache.pop(next(iter(_coordinate_cache)))
            _coordinate_cache[item["label"].casefold()] = (float(item["longitude"]), float(item["latitude"]), item["label"])
    return suggestions


@router.get("")
async def list_vehicles(_: dict = Depends(require_permission("RECEIPTS_READ"))):
    db = get_database()
    vehicles = [item async for item in db.vehicles.find({"deleted_at": None}).sort("name", 1)]
    if not vehicles:
        return []
    vehicle_ids = [item["_id"] for item in vehicles]
    receipt_query = {"deleted_at": None, "document_type": "receipt", "category": "gas",
                     "vehicle_id": {"$in": vehicle_ids}, "fuel_litres": {"$ne": None}}
    trip_query = {"deleted_at": None, "vehicle_id": {"$in": vehicle_ids}}
    receipts, trips = await asyncio.gather(
        db.receipts.find(receipt_query).to_list(length=None),
        db.vehicle_trips.find(trip_query).to_list(length=None),
    )
    receipts_by_vehicle: dict[UUID, list[dict]] = {vehicle_id: [] for vehicle_id in vehicle_ids}
    trips_by_vehicle: dict[UUID, list[dict]] = {vehicle_id: [] for vehicle_id in vehicle_ids}
    for receipt in receipts:
        receipts_by_vehicle.setdefault(receipt["vehicle_id"], []).append(receipt)
    for trip in trips:
        trips_by_vehicle.setdefault(trip["vehicle_id"], []).append(trip)
    result = []
    for vehicle in vehicles:
        vehicle_receipts = receipts_by_vehicle[vehicle["_id"]]
        vehicle_trips = trips_by_vehicle[vehicle["_id"]]
        result.append({**serialize(vehicle), "fuel": fuel_summary(vehicle, vehicle_receipts, vehicle_trips),
                       "receipt_usage": receipt_usage(vehicle, vehicle_receipts, vehicle_trips),
                       "insurance": insurance_summary(vehicle, vehicle_trips, date.today().year)})
    return result


@router.post("", status_code=201)
async def create_vehicle(payload: VehicleCreate, actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    now = utcnow()
    has_default = await get_database().vehicles.find_one({"deleted_at": None, "is_default": True})
    vehicle = {"_id": uuid4(), "name": payload.name.strip(), "make": payload.make.strip(),
               "model": payload.model.strip(), "year": payload.year,
               "fuel_efficiency_l_per_100km": payload.fuel_efficiency_l_per_100km,
               "monthly_insurance_cost": payload.monthly_insurance_cost,
               "annual_total_km": payload.annual_total_km,
               "is_default": has_default is None, "created_by": actor["_id"], "created_at": now,
               "updated_at": now, "deleted_at": None}
    await get_database().vehicles.insert_one(vehicle)
    await write_audit("VEHICLE_CREATED", actor["_id"], "vehicle", vehicle["_id"])
    return serialize(vehicle)


@router.patch("/{vehicle_id}/insurance")
async def update_vehicle_insurance(vehicle_id: UUID, payload: InsuranceUpdate,
                                   actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    db = get_database()
    vehicle = await db.vehicles.find_one({"_id": vehicle_id, "deleted_at": None})
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    now = utcnow()
    changes = {"monthly_insurance_cost": payload.monthly_insurance_cost,
               "updated_at": now}
    await db.vehicles.update_one({"_id": vehicle_id}, {"$set": changes})
    await write_audit("VEHICLE_INSURANCE_UPDATED", actor["_id"], "vehicle", vehicle_id)
    return serialize({**vehicle, **changes})


@router.patch("/{vehicle_id}/default")
async def set_default_vehicle(vehicle_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    db = get_database()
    vehicle = await db.vehicles.find_one({"_id": vehicle_id, "deleted_at": None})
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    now = utcnow()
    await db.vehicles.update_many({"deleted_at": None}, {"$set": {"is_default": False, "updated_at": now}})
    await db.vehicles.update_one({"_id": vehicle_id}, {"$set": {"is_default": True, "updated_at": now}})
    await write_audit("DEFAULT_VEHICLE_CHANGED", actor["_id"], "vehicle", vehicle_id)
    vehicle["is_default"] = True
    vehicle["updated_at"] = now
    return serialize(vehicle)


@router.delete("/{vehicle_id}", status_code=204)
async def delete_vehicle(vehicle_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_DELETE"))):
    db = get_database()
    vehicle = await db.vehicles.find_one({"_id": vehicle_id, "deleted_at": None})
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    now = utcnow()
    await db.vehicles.update_one({"_id": vehicle_id},
                                 {"$set": {"deleted_at": now, "updated_at": now, "is_default": False}})
    if vehicle.get("is_default"):
        replacement = await db.vehicles.find_one({"_id": {"$ne": vehicle_id}, "deleted_at": None},
                                                 sort=[("created_at", 1)])
        if replacement:
            await db.vehicles.update_one({"_id": replacement["_id"]},
                                         {"$set": {"is_default": True, "updated_at": now}})
    await write_audit("VEHICLE_DELETED", actor["_id"], "vehicle", vehicle_id)


@router.post("/{vehicle_id}/trips", status_code=201)
async def create_trip(vehicle_id: UUID, payload: TripCreate,
                      actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    db = get_database()
    vehicle = await db.vehicles.find_one({"_id": vehicle_id, "deleted_at": None})
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    receipts, trips = await asyncio.gather(
        db.receipts.find({"vehicle_id": vehicle_id, "deleted_at": None, "document_type": "receipt",
                          "category": "gas", "fuel_litres": {"$ne": None}}).to_list(length=None),
        db.vehicle_trips.find({"vehicle_id": vehicle_id, "deleted_at": None}).to_list(length=None),
    )
    if not receipts:
        raise HTTPException(422, "Add a gas receipt for this vehicle before logging trips")
    usage = receipt_usage(vehicle, receipts, trips)
    current_receipt = next((UUID(row["receipt_id"]) for row in usage if row["remaining_litres"] > 0), None)
    now = utcnow()
    trip = {"_id": uuid4(), "vehicle_id": vehicle_id, "receipt_id": current_receipt,
            "distance_km": payload.distance_km, "purpose": payload.purpose,
            "occurred_at": datetime.combine(payload.occurred_at, time.min, tzinfo=timezone.utc),
            "notes": payload.notes.strip(), "start_location": payload.start_location.strip(),
            "end_location": payload.end_location.strip(), "created_by": actor["_id"], "created_at": now,
            "updated_at": now, "deleted_at": None}
    await db.vehicle_trips.insert_one(trip)
    await write_audit("VEHICLE_TRIP_CREATED", actor["_id"], "vehicle_trip", trip["_id"])
    result = serialize(trip)
    result["spreadsheet_sync"] = await sync_safely(trip)
    return result


@router.post("/{vehicle_id}/trips/{trip_id}/sync-spreadsheet")
async def retry_trip_spreadsheet(vehicle_id: UUID, trip_id: UUID,
                                actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    trip = await get_database().vehicle_trips.find_one({"_id": trip_id, "vehicle_id": vehicle_id, "deleted_at": None})
    if trip is None:
        raise HTTPException(404, "Trip not found")
    result = await sync_safely(trip)
    await write_audit("TRIP_SPREADSHEET_SYNC", actor["_id"], "vehicle_trip", trip_id)
    return result


async def _geocode(client: httpx.AsyncClient, address: str) -> tuple[float, float, str]:
    cached = _coordinate_cache.get(address.casefold())
    if cached is not None:
        return cached
    results = await place_suggestions(address)
    street_number = address.split(maxsplit=1)[0] if address[:1].isdigit() else ""
    match = next((item for item in results if not street_number or
                  (item["label"].startswith(street_number + " ") and
                   _street_tokens(address) <= _street_tokens(item["label"]))), None)
    if match is None:
        raise HTTPException(422, f"Location not found: {address}. Select an address suggestion or add city and province.")
    result = (float(match["longitude"]), float(match["latitude"]), match["label"])
    _coordinate_cache[address.casefold()] = result
    return result


@router.post("/route-distance")
async def route_distance(payload: RouteRequest, _: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    try:
        client = _client()
        start, end = await asyncio.gather(
            _geocode(client, payload.start_location.strip()),
            _geocode(client, payload.end_location.strip()),
        )
        route = await client.get(f"https://router.project-osrm.org/route/v1/driving/{start[0]},{start[1]};{end[0]},{end[1]}",
                                 params={"overview": "false", "alternatives": "false"}, timeout=8)
        route.raise_for_status()
        routes = route.json().get("routes", [])
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Driving distance service is unavailable") from exc
    if not routes:
        raise HTTPException(422, "No driving route was found between these locations")
    return {"distance_km": round(routes[0]["distance"] / 1000, 1),
            "start_location": start[2], "end_location": end[2],
            "provider": "Natural Resources Canada / OSRM"}
