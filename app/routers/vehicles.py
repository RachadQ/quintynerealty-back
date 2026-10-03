"""Vehicle fuel balances calculated from gas receipts and logged trips."""

import asyncio
import csv
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import httpx

from app.audit import write_audit
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission

router = APIRouter(prefix="/vehicles", tags=["vehicles"])
CATALOG_URLS = {
    "2026": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/9df1b18d-d036-4783-a61c-99f1f75b3ac5/download/my2026-fuel-consumption-ratings.csv",
    "2025": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/d589f2bc-9a85-4f65-be2f-20f17debfcb1/download/my2025-fuel-consumption-ratings.csv",
    "2015-2024": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/c98b9dc8-b23f-4cd8-8b19-e892da1e4688/download/my2015-2024-fuel-consumption-ratings.csv",
    "1995-2014": "https://open.canada.ca/data/dataset/98f1a129-f628-4ce4-b24d-6f16bf24dd64/resource/42495676-28b7-40f3-b0e0-3d7fe005ca56/download/my1995-2014-fuel-consumption-ratings-5-cycle.csv",
}
_catalog_cache: dict[str, list[dict]] = {}
_place_cache: dict[str, list[dict]] = {}
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
    annual_total_km: float = Field(gt=0, le=1000000, allow_inf_nan=False)


class TripCreate(BaseModel):
    receipt_id: UUID
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
    efficiency = Decimal(str(vehicle["fuel_efficiency_l_per_100km"]))
    grouped: dict[UUID, list[dict]] = {}
    for trip in trips:
        if trip.get("receipt_id"):
            grouped.setdefault(trip["receipt_id"], []).append(trip)
    result = []
    for receipt in receipts:
        linked = grouped.get(receipt["_id"], [])
        distance = sum((Decimal(str(item["distance_km"])) for item in linked), Decimal("0"))
        used = distance * efficiency / Decimal("100")
        purchased = Decimal(str(receipt.get("fuel_litres") or 0))
        remaining = purchased - used
        result.append({"receipt_id": str(receipt["_id"]), "trip_count": len(linked),
                       "distance_km": float(distance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                       "used_litres": float(used.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                       "remaining_litres": float(remaining.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))})
    return result


def insurance_summary(vehicle: dict, trips: list[dict], year: int) -> dict:
    monthly = Decimal(str(vehicle.get("monthly_insurance_cost") or 0))
    total_km = Decimal(str(vehicle.get("annual_total_km") or 0))
    business_km = sum((Decimal(str(item["distance_km"])) for item in trips
                       if item.get("occurred_at") and item["occurred_at"].year == year), Decimal("0"))
    annual_cost = monthly * Decimal("12")
    ratio = min(business_km / total_km, Decimal("1")) if total_km > 0 else Decimal("0")
    money = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"year": year, "monthly_cost": money(monthly), "annual_cost": money(annual_cost),
            "annual_total_km": money(total_km), "business_km": money(business_km),
            "business_use_percent": money(ratio * Decimal("100")),
            "deductible_amount": money(annual_cost * ratio), "currency": "CAD"}


async def _catalog(year: int) -> list[dict]:
    key = str(year) if year >= 2025 else "2015-2024" if year >= 2015 else "1995-2014"
    if key not in _catalog_cache:
        try:
            response = await _client().get(CATALOG_URLS[key], timeout=30, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Canadian vehicle ratings are temporarily unavailable") from exc
        rows = csv.DictReader(StringIO(response.content.decode("utf-8-sig", errors="replace").replace("\r\r\n", "\n")))
        _catalog_cache[key] = [{"year": int(row["Model year"]), "make": row["Make"].strip(),
            "model": row["Model"].strip(), "combined": float(row["Combined (L/100 km)"])}
            for row in rows if row.get("Model year") and row.get("Combined (L/100 km)")]
    return [item for item in _catalog_cache[key] if item["year"] == year]


@router.get("/catalog")
async def vehicle_catalog(year: int, make: str | None = None,
                          _: dict = Depends(require_permission("RECEIPTS_READ"))):
    if year < 1995 or year > 2026:
        raise HTTPException(422, "Canadian ratings are available for model years 1995 through 2026")
    records = await _catalog(year)
    if not make:
        return {"year": year, "makes": sorted({item["make"] for item in records})}
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
    deadline = asyncio.get_running_loop().time() + 2.8
    photon_task = asyncio.create_task(_client().get(
        "https://photon.komoot.io/api/",
        params={"q": provider_query, "limit": 6, "lang": "en", "bbox": "-141,41,-52,84"},
        timeout=httpx.Timeout(3.0, connect=1.5),
    )) if street_number else None
    try:
        timeout = httpx.Timeout(3.0, connect=1.5)
        response = await asyncio.wait_for(_client().get("https://geolocator.api.geo.ca/",
            params={"q": provider_query, "lang": "en", "keys": "locate,fsa"}, timeout=timeout),
            timeout=max(0.1, deadline - asyncio.get_running_loop().time()))
        response.raise_for_status()
    except (httpx.HTTPError, TimeoutError):
        response = None
    suggestions = []
    for item in response.json()[:8] if response is not None else []:
        label = str(item.get("name") or "").strip()
        if label and item.get("lat") is not None and item.get("lng") is not None:
            suggestions.append({"label": label, "longitude": item["lng"],
                                "latitude": item["lat"], "provider": "Natural Resources Canada"})
    if street_number and not any(item["label"].startswith(street_number) for item in suggestions):
        try:
            fallback = await asyncio.wait_for(photon_task,
                timeout=max(0.1, deadline - asyncio.get_running_loop().time()))
            fallback.raise_for_status()
            exact = []
            for feature in fallback.json().get("features", []):
                props = feature.get("properties", {})
                if str(props.get("housenumber") or "") != street_number:
                    continue
                coordinates = feature.get("geometry", {}).get("coordinates", [])
                parts = [f'{props.get("housenumber", "")} {props.get("street", "")}'.strip(),
                         props.get("city") or props.get("district"), props.get("state")]
                label = ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))
                if label and len(coordinates) == 2:
                    exact.append({"label": label, "longitude": coordinates[0],
                                  "latitude": coordinates[1], "provider": "Photon address fallback"})
            if exact:
                street_tokens = _street_tokens(provider_query)
                preferred = [item for item in exact
                             if street_tokens <= _street_tokens(item["label"])]
                unique = {item["label"]: item for item in reversed(preferred)}
                suggestions = list(unique.values()) + suggestions
        except (httpx.HTTPError, TimeoutError):
            pass
    elif photon_task is not None:
        photon_task.cancel()
        await asyncio.gather(photon_task, return_exceptions=True)
    if len(_place_cache) >= 250:
        _place_cache.pop(next(iter(_place_cache)))
    _place_cache[cache_key] = suggestions
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
               "annual_total_km": payload.annual_total_km, "updated_at": now}
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
    if await db.vehicles.find_one({"_id": vehicle_id, "deleted_at": None}) is None:
        raise HTTPException(404, "Vehicle not found")
    receipt = await db.receipts.find_one({"_id": payload.receipt_id, "deleted_at": None,
                                          "document_type": "receipt", "category": "gas"})
    if receipt is None:
        raise HTTPException(404, "Gas receipt not found")
    if receipt.get("vehicle_id") != vehicle_id:
        raise HTTPException(422, "The gas receipt belongs to a different vehicle")
    now = utcnow()
    trip = {"_id": uuid4(), "vehicle_id": vehicle_id, "receipt_id": payload.receipt_id,
            "distance_km": payload.distance_km,
            "occurred_at": datetime.combine(payload.occurred_at, time.min, tzinfo=timezone.utc),
            "notes": payload.notes.strip(), "start_location": payload.start_location.strip(),
            "end_location": payload.end_location.strip(), "created_by": actor["_id"], "created_at": now,
            "updated_at": now, "deleted_at": None}
    await db.vehicle_trips.insert_one(trip)
    await write_audit("VEHICLE_TRIP_CREATED", actor["_id"], "vehicle_trip", trip["_id"])
    return serialize(trip)


async def _geocode(client: httpx.AsyncClient, address: str) -> tuple[float, float, str]:
    response = await client.get("https://geolocator.api.geo.ca/",
        params={"q": address, "lang": "en", "keys": "locate,fsa"})
    response.raise_for_status()
    results = response.json()
    if not results:
        raise HTTPException(422, f"Location not found: {address}")
    street_number = address.split(maxsplit=1)[0] if address[:1].isdigit() else ""
    match = next((item for item in results if not street_number
                  or str(item.get("name") or "").startswith(street_number)), None)
    if match:
        return float(match["lng"]), float(match["lat"]), match["name"]
    fallback = await client.get("https://photon.komoot.io/api/",
        params={"q": address, "limit": 1, "lang": "en", "bbox": "-141,41,-52,84"})
    fallback.raise_for_status()
    features = fallback.json().get("features", [])
    if not features:
        raise HTTPException(422, f"Location not found: {address}")
    feature = features[0]
    props = feature.get("properties", {})
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    if len(coordinates) != 2:
        raise HTTPException(422, f"Location not found: {address}")
    parts = [f'{props.get("housenumber", "")} {props.get("street", "")}'.strip(),
             props.get("city") or props.get("district"), props.get("state")]
    label = ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))
    return float(coordinates[0]), float(coordinates[1]), label or address


@router.post("/route-distance")
async def route_distance(payload: RouteRequest, _: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    headers = {"User-Agent": "QuintyneRealtyIncAdministration/1.0 (vehicle trip distance lookup)"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=False) as client:
            start, end = await asyncio.gather(
                _geocode(client, payload.start_location.strip()),
                _geocode(client, payload.end_location.strip()),
            )
            route = await client.get(f"https://router.project-osrm.org/route/v1/driving/{start[0]},{start[1]};{end[0]},{end[1]}",
                                     params={"overview": "false", "alternatives": "false"})
            route.raise_for_status()
            routes = route.json().get("routes", [])
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Driving distance service is unavailable") from exc
    if not routes:
        raise HTTPException(422, "No driving route was found between these locations")
    return {"distance_km": round(routes[0]["distance"] / 1000, 1),
            "start_location": start[2], "end_location": end[2],
            "provider": "Natural Resources Canada / OSRM"}
