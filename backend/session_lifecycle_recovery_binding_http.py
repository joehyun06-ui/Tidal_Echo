"""Opt-in HTTP projection of a verified recovery binding."""
from __future__ import annotations

import asyncio
import hashlib
import re

from fastapi import FastAPI, HTTPException, Request

from .g3_projection_contract import AccessScope
from .g3_projection_feed import _binding
from .session_lifecycle import _session_id
from .session_lifecycle_status_context import RecoveryStatusContextProvider
from .session_lifecycle_status_proxy import (
    TIMEOUT_SECONDS, _context, _response, _valid_origin,
)


PATH = '/app/sessions/{conversation_id}/lifecycle/activation/binding'


def install(app, *, enabled=False, provider=None, expected_access=None,
            expected_origin=None):
    if enabled is not True:
        return False
    if (not isinstance(app, FastAPI) or app.middleware_stack is not None
            or type(provider) is not RecoveryStatusContextProvider
            or type(expected_access) is not AccessScope
            or not _valid_origin(expected_origin)):
        raise ValueError('lifecycle_recovery_binding_invalid')
    expected_binding = _binding(expected_access)
    normalized_path = re.sub(r'\{[^}]+\}', '{}', PATH)
    if any(re.sub(r'\{[^}]+\}', '{}', getattr(route, 'path', '')) == normalized_path
           for route in app.routes):
        raise ValueError('lifecycle_recovery_binding_route_conflict')

    @app.post(PATH, include_in_schema=False)
    async def recovery_binding(request: Request, conversation_id: str):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                if request.headers.getlist('origin') != [expected_origin]:
                    return _response(403, 'access_denied')
                if len(request.headers.getlist('authorization')) > 1:
                    return _response(401, 'auth_required')
                if (request.scope.get('query_string', b'') or not _session_id(conversation_id)
                        or request.headers.getlist('x-api-loop-internal-token')):
                    return _response(400, 'invalid_request')
                async for chunk in request.stream():
                    if chunk:
                        return _response(400, 'invalid_request')
                result = await provider.binding(request, conversation_id)
                _, _, epoch, version = _context(result.context, conversation_id, expected_binding)
                # Both components are server-generated HMAC labels. Fold them
                # together so a re-login invalidates a captured restore scope
                # without exposing the individual labels.
                revision = hashlib.sha256(f'{epoch}\n{version}'.encode('ascii')).hexdigest()
                return _response(200, body={
                    'identity': result.identity,
                    'conversationId': conversation_id,
                    'contextVersion': 'recovery-context:' + revision,
                })
        except HTTPException as error:
            code = error.status_code
            if code == 401:
                return _response(401, 'auth_required')
            if code == 403:
                return _response(403, 'access_denied')
            return _response(503)
        except Exception:
            return _response(503)

    return True
