"""Synchronize the app-managed annual trip log with OneDrive."""
import asyncio
import csv
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.database import get_database
from app.models import utcnow
from app.services import onedrive

_locks = {}


def csv_text(value) -> str:
    """Treat user-entered spreadsheet text as data, never executable formulas."""
    text = str(value or '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def render_csv(trips: list[dict], vehicles: dict) -> bytes:
    output = StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Date', 'Vehicle', 'Trip type', 'From', 'To', 'Distance (km)',
                     'Estimated fuel used (L)', 'Notes', 'Trip ID'])
    for trip in trips:
        vehicle = vehicles.get(trip['vehicle_id'], {})
        fuel = Decimal(str(trip['distance_km'])) * Decimal(str(vehicle.get('fuel_efficiency_l_per_100km') or 0)) / Decimal('100')
        writer.writerow([trip['occurred_at'].strftime('%Y-%m-%d'), csv_text(vehicle.get('name') or 'Archived vehicle'),
                         trip.get('purpose', 'business').title(), csv_text(trip.get('start_location')),
                         csv_text(trip.get('end_location')), trip['distance_km'],
                         str(fuel.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
                         csv_text(trip.get('notes')), str(trip['_id'])])
    return output.getvalue().encode('utf-8-sig')


async def sync(year: int, month: int) -> dict:
    if not await onedrive.connected():
        return {'status': 'not_connected', 'message': 'Trip saved. Connect OneDrive from Receipts to sync the trip spreadsheet.'}
    key = f'trip-log:{year}'
    async with _locks.setdefault(key, asyncio.Lock()):
        db = get_database()
        owner = uuid4().hex
        try:
            lease = await db.cloud_sync_locks.find_one_and_update(
                {'_id': key, '$or': [{'expires_at': {'$lte': utcnow()}}, {'expires_at': {'$exists': False}}]},
                {'$set': {'owner': owner, 'expires_at': utcnow() + timedelta(minutes=3)}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            return {'status': 'failed', 'message': 'Trip saved. Another spreadsheet update is running; retry sync in a moment.'}
        if lease is None:
            return {'status': 'failed', 'message': 'Trip saved. Retry spreadsheet sync in a moment.'}
        try:
            trips = await db.vehicle_trips.find({'deleted_at': None, 'occurred_at': {
                '$gte': datetime(year, 1, 1, tzinfo=timezone.utc),
                '$lt': datetime(year + 1, 1, 1, tzinfo=timezone.utc)}}).sort([('occurred_at', 1), ('created_at', 1), ('_id', 1)]).to_list(length=None)
            vehicles = {item['_id']: item async for item in db.vehicles.find({})}
            content = await asyncio.to_thread(render_csv, trips, vehicles)
            token, connection = await onedrive.access_token()
            log_id = f'{connection["drive_id"]}:{key}'
            log = await db.cloud_trip_logs.find_one({'_id': log_id})
            if log is None:
                item = await onedrive.upload(content, f'Trip Log {year}.csv', 'text/csv; charset=utf-8',
                    [str(year)])
                log = {'_id': log_id, **item}
                await db.cloud_trip_logs.update_one({'_id': log_id}, {'$set': {key: value for key, value in log.items() if key != '_id'}}, upsert=True)
            else:
                async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                    path = f'/drives/{quote(log["drive_id"], safe="")}/items/{quote(log["file_id"], safe="")}/content'
                    response = await onedrive.graph_request(client, token, 'PUT', path,
                        content=content, headers={'Content-Type': 'text/csv; charset=utf-8'})
                    if response.status_code not in {200, 201}:
                        raise HTTPException(502, 'Trip spreadsheet is unavailable. The trip remains saved.')
            filename = f'Trip Log {year}.csv'
            if log.get('filename') != filename:
                renamed = await onedrive.rename_file(log['drive_id'], log['file_id'], filename)
                await db.cloud_trip_logs.update_one({'_id': log_id}, {'$set': renamed})
                log.update(renamed)
            await db.cloud_trip_logs.update_one({'_id': log_id}, {'$set': {'updated_at': utcnow(), 'trip_count': len(trips)}})
            result = {'status': 'synced', 'message': f'{year} trip spreadsheet updated in OneDrive.'}
            if log.get('web_url'):
                result.update(web_url=log['web_url'], storage_provider='onedrive')
            return result

        finally:
            await db.cloud_sync_locks.delete_one({'_id': key, 'owner': owner})


async def sync_safely(trip: dict) -> dict:
    try:
        return await sync(trip['occurred_at'].year, trip['occurred_at'].month)
    except Exception:
        # Saving a trip must succeed even if cloud storage is temporarily unavailable.
        return {'status': 'failed', 'message': 'Trip saved, but the OneDrive spreadsheet could not be updated. Retry spreadsheet sync.'}
