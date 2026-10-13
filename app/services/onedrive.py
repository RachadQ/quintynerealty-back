"""OneDrive receipt storage with encrypted, server-side OAuth tokens."""
import asyncio
import base64
import hashlib
import json
import logging
import re
from datetime import timedelta, timezone
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app.config import get_settings
from app.database import get_database
from app.models import utcnow

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access https://graph.microsoft.com/Files.ReadWrite"
CONNECTION_ID = "onedrive"
_refresh_lock = asyncio.Lock()


class RedactDownloadUrl(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            args = []
            for value in record.args:
                rendered = str(value)
                if rendered.startswith('https://'):
                    host = urlparse(rendered).hostname or ''
                    if host == 'my.microsoftpersonalcontent.com' or any(host.endswith(suffix) for suffix in ('.sharepoint.com', '.1drv.com', '.storage.live.com')):
                        value = '[OneDrive download URL]'
                if rendered.startswith(('http://', 'https://')):
                    parsed = urlparse(rendered)
                    if parsed.path == '/auth/microsoft/callback':
                        value = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
                args.append(value)
            record.args = tuple(args)
        return True


logging.getLogger('httpx').addFilter(RedactDownloadUrl())
logging.getLogger('httpx2').addFilter(RedactDownloadUrl())


def cipher() -> Fernet:
    key = hashlib.sha256((get_settings().session_secret + ':onedrive').encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_tokens(tokens: dict) -> str:
    return cipher().encrypt(json.dumps(tokens).encode()).decode()


def decrypt_tokens(value: str) -> dict:
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise HTTPException(503, "OneDrive connection needs to be renewed") from None


def authority() -> str:
    tenant = get_settings().microsoft_tenant_id
    if not tenant or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in tenant):
        raise HTTPException(503, "Invalid Microsoft tenant configuration")
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def callback_origin() -> str:
    settings = get_settings()
    parsed = urlparse(settings.microsoft_redirect_uri)
    if (not settings.microsoft_client_id or not settings.microsoft_client_secret or
            parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or
            parsed.path != '/auth/microsoft/callback' or parsed.query or parsed.fragment or
            parsed.hostname not in settings.hosts):
        raise HTTPException(503, "Configure Microsoft credentials and the /auth/microsoft/callback redirect URI")
    if parsed.scheme != 'https' and (settings.app_env != 'development' or parsed.hostname not in {'localhost', '127.0.0.1'}):
        raise HTTPException(503, "Microsoft callback must use HTTPS outside localhost development")
    return f'{parsed.scheme}://{parsed.netloc}'


def safe_web_url(value: str) -> str | None:
    parsed = urlparse(value)
    host = parsed.hostname or ''
    if (parsed.scheme == 'https' and not parsed.username and not parsed.password and
            (host in {'onedrive.live.com', '1drv.ms'} or host.endswith('.sharepoint.com'))):
        return value
    return None


def token_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {}
    except ValueError:
        body = {}
    values = body.get('error_codes', [])
    codes = [str(value) for value in values if type(value) is int and 0 < value < 1_000_000_000] if isinstance(values, list) else []
    if not codes:
        codes = re.findall(r'\bAADSTS(\d{3,9})\b', str(body.get('error_description', '')))
    codes = list(dict.fromkeys(codes))[:4]
    hints = {
        '7000215': 'MICROSOFT_CLIENT_SECRET is invalid. Use the secret Value from the same app registration, not its Secret ID, then redeploy.',
        '7000222': 'The Microsoft client secret expired. Create a new secret, update MICROSOFT_CLIENT_SECRET, and redeploy.',
        '700016': 'MICROSOFT_CLIENT_ID or MICROSOFT_TENANT_ID does not match the Microsoft app registration.',
        '700025': 'This app is configured as a public client. Register the callback under the Web platform for the backend flow.',
        '50011': 'MICROSOFT_REDIRECT_URI must exactly match the Web redirect URI in the same app registration.',
        '9002327': 'The callback was registered as a Single-page application. Register it under Web for this backend flow.',
        '9002326': 'Check that the callback is registered under Web, not Single-page application.',
        '70000': 'The authorization code or refresh token is invalid or expired. Start a fresh OneDrive connection from Receipts.',
    }
    code_text = ', '.join('AADSTS' + code for code in codes)
    hint = next((hints[code] for code in codes if code in hints), 'Check the Microsoft app settings and start a fresh OneDrive connection from Receipts.')
    # Never return Microsoft's raw description: it can contain account/client details.
    return 'Microsoft authorization failed' + (f' ({code_text})' if code_text else f' (HTTP {response.status_code})') + '. ' + hint


async def token_request(data: dict) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        try:
            response = await client.post(authority() + '/token', data={
                'client_id': settings.microsoft_client_id, 'client_secret': settings.microsoft_client_secret,
                **data,
            })
        except httpx.HTTPError:
            raise HTTPException(502, "Microsoft connection service is unavailable") from None
    if response.status_code != 200:
        detail = token_error_detail(response)
        logging.getLogger(__name__).warning('%s', detail)
        raise HTTPException(503, detail)
    tokens = response.json()
    if not tokens.get('access_token'):
        raise HTTPException(503, "Microsoft did not return an access token")
    return tokens


async def connected() -> bool:
    return await get_database().cloud_connections.find_one({'_id': CONNECTION_ID}, {'_id': 1}) is not None


async def access_token() -> tuple[str, dict]:
    async with _refresh_lock:
        connection = await get_database().cloud_connections.find_one({'_id': CONNECTION_ID})
        if connection is None:
            raise HTTPException(503, "Connect OneDrive before uploading receipts")
        tokens = decrypt_tokens(connection['encrypted_tokens'])
        expires = connection['expires_at'].replace(tzinfo=timezone.utc)
        if expires <= utcnow() + timedelta(seconds=60):
            refreshed = await token_request({'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'], 'scope': SCOPES})
            refreshed.setdefault('refresh_token', tokens['refresh_token'])
            await get_database().cloud_connections.update_one({'_id': CONNECTION_ID}, {'$set': {
                'encrypted_tokens': encrypt_tokens(refreshed),
                'expires_at': utcnow() + timedelta(seconds=int(refreshed.get('expires_in', 3600))),
                'updated_at': utcnow(),
            }})
            tokens = refreshed
        return tokens['access_token'], connection


async def graph_request(client: httpx.AsyncClient, token: str, method: str, path: str, **kwargs) -> httpx.Response:
    try:
        response = await client.request(method, GRAPH + path, headers={'Authorization': f'Bearer {token}', **kwargs.pop('headers', {})}, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(502, "OneDrive storage is temporarily unavailable") from None
    if response.status_code not in {200, 201, 204, 404, 409}:
        raise HTTPException(502, f"OneDrive request failed (HTTP {response.status_code}). Reconnect if access has expired")
    return response


async def ensure_folder(client: httpx.AsyncClient, token: str, drive_id: str, parent: str, name: str) -> str:
    base = f'/drives/{quote(drive_id, safe="")}/items/{quote(parent, safe="")}'
    path = base + ':/' + quote(name, safe='')
    response = await graph_request(client, token, 'GET', path)
    if response.status_code == 404:
        response = await graph_request(client, token, 'POST', base + '/children', json={
            'name': name, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail',
        })
        if response.status_code == 409:
            response = await graph_request(client, token, 'GET', path)
    if response.status_code not in {200, 201} or 'folder' not in response.json():
        raise HTTPException(502, "OneDrive receipt folder is unavailable")
    return response.json()['id']


async def establish(tokens: dict, user_id) -> None:
    if not tokens.get('refresh_token'):
        raise HTTPException(503, "Microsoft did not grant offline access. Reconnect and approve access")
    token = tokens['access_token']
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        drive_response = await graph_request(client, token, 'GET', '/me/drive')
        root_response = await graph_request(client, token, 'GET', '/me/drive/root')
        if drive_response.status_code != 200 or root_response.status_code != 200:
            raise HTTPException(503, "This Microsoft account does not have an available OneDrive")
        drive = drive_response.json()
        root = await ensure_folder(client, token, drive['id'], root_response.json()['id'], 'Quintyne Realty')
    await get_database().cloud_connections.update_one({'_id': CONNECTION_ID}, {'$set': {
        'drive_id': drive['id'], 'root_folder_id': root, 'connected_by': user_id,
        'encrypted_tokens': encrypt_tokens(tokens),
        'expires_at': utcnow() + timedelta(seconds=int(tokens.get('expires_in', 3600))),
        'updated_at': utcnow(),
    }}, upsert=True)


async def upload(content: bytes, filename: str, mime_type: str, folders: list[str]) -> dict:
    token, connection = await access_token()
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        parent = connection['root_folder_id']
        for folder in folders:
            parent = await ensure_folder(client, token, connection['drive_id'], parent, folder)
        unique_name = f'{uuid4().hex}-{filename}'
        path = f'/drives/{quote(connection["drive_id"], safe="")}/items/{quote(parent, safe="")}:/{quote(unique_name, safe="") }:/content'
        response = await graph_request(client, token, 'PUT', path, content=content, headers={'Content-Type': mime_type})
        if response.status_code not in {200, 201}:
            raise HTTPException(502, "OneDrive receipt upload failed")
        item = response.json()
    result = {'file_id': item['id'], 'drive_id': connection['drive_id']}
    url = safe_web_url(item.get('webUrl', ''))
    if url:
        result['web_url'] = url
    return result


async def rename_file(drive_id: str, file_id: str, filename: str) -> dict:
    token, _ = await access_token()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await graph_request(client, token, 'PATCH',
            f'/drives/{quote(drive_id, safe="")}/items/{quote(file_id, safe="")}', json={'name': filename})
    if response.status_code == 409:
        raise HTTPException(409, 'A different OneDrive file already has this name. No file was overwritten')
    if response.status_code != 200:
        raise HTTPException(502, 'OneDrive file could not be renamed')
    item = response.json()
    result = {'filename': item['name']}
    url = safe_web_url(item.get('webUrl', ''))
    if url:
        result['web_url'] = url
    return result


async def download(drive_id: str, file_id: str) -> bytes:
    token, _ = await access_token()
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        metadata = await graph_request(client, token, 'GET', f'/drives/{quote(drive_id, safe="")}/items/{quote(file_id, safe="")}')
        if metadata.status_code != 200:
            raise HTTPException(404, "OneDrive receipt was not found")
        url = metadata.json().get('@microsoft.graph.downloadUrl', '')
        parsed = urlparse(url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or parsed.username or parsed.password or not (
                host == 'my.microsoftpersonalcontent.com' or host.endswith('.sharepoint.com') or host.endswith('.1drv.com') or host.endswith('.files.1drv.com') or host.endswith('.storage.live.com')):
            raise HTTPException(502, "OneDrive returned an unsupported download address")
        try:
            # Preauthenticated URL: never forward the Graph bearer token.
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(502, "OneDrive receipt download failed") from None
        return response.content
