import base64
import hashlib
import secrets
import time
import logging
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.security import get_current_user
from app.services import onedrive

router = APIRouter(tags=['OneDrive storage'])


class RedactCallbackQuery(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split('?')[0] == '/auth/microsoft/callback':
                args[2] = '/auth/microsoft/callback'
                record.args = tuple(args)
        return True


logging.getLogger('uvicorn.access').addFilter(RedactCallbackQuery())


async def admin(user: dict = Depends(get_current_user)) -> dict:
    if not user.get('is_super_admin'):
        raise HTTPException(403, 'Only a super administrator can connect company storage')
    return user


@router.get('/storage/onedrive/status')
async def status(user: dict = Depends(get_current_user)):
    settings = get_settings()
    configured = bool(settings.microsoft_client_id and settings.microsoft_client_secret and settings.microsoft_redirect_uri)
    url = None
    if configured and user.get('is_super_admin'):
        url = onedrive.callback_origin() + '/storage/onedrive/connect'
    return {'configured': configured, 'connected': await onedrive.connected(), 'connect_url': url}


@router.get('/storage/onedrive/connect')
async def connect(request: Request, user: dict = Depends(admin)):
    onedrive.callback_origin()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    request.session['onedrive_oauth'] = {'state': state, 'verifier': verifier, 'created': time.time(), 'user_id': str(user['_id'])}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return RedirectResponse(onedrive.authority() + '/authorize?' + urlencode({
        'client_id': get_settings().microsoft_client_id, 'redirect_uri': get_settings().microsoft_redirect_uri,
        'response_type': 'code', 'response_mode': 'query', 'scope': onedrive.SCOPES,
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account',
    }), status_code=303)


@router.get('/auth/microsoft/callback')
async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    pending = request.session.pop('onedrive_oauth', None)
    if not pending or not state or not secrets.compare_digest(state, pending['state']) or time.time() - pending['created'] > 600:
        raise HTTPException(400, 'OneDrive connection expired or could not be verified. Start again from Receipts')
    settings = get_settings()
    if error or not code:
        return RedirectResponse(settings.frontend_url + '/?onedrive=cancelled', status_code=303)
    user_id = UUID(pending['user_id'])
    local_admin = settings.app_env == 'development' and settings.dev_auth_bypass and str(user_id) == '00000000-0000-4000-8000-000000000001'
    if not local_admin and await get_database().users.find_one({'_id': user_id, 'is_super_admin': True, 'is_active': True, 'deleted_at': None}) is None:
        raise HTTPException(403, 'Administrator access is no longer available')
    tokens = await onedrive.token_request({'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': settings.microsoft_redirect_uri, 'code_verifier': pending['verifier'], 'scope': onedrive.SCOPES})
    await onedrive.establish(tokens, user_id)
    await write_audit('ONEDRIVE_CONNECTED', user_id, 'cloud_storage')
    return RedirectResponse(settings.frontend_url + '/?onedrive=connected', status_code=303)
