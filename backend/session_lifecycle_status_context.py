"""Default-off request-scoped recovery context provider, no login or bootstrap.

Reuses durable auth epochs and coordinated canonical read transactions. Hosts
must still authenticate each request and authorize principal/root/version.
No shared-secret promotion, browser-issued identity, new DB, schema or route.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import inspect
from pathlib import Path

from fastapi import HTTPException, Request

from .g3_projection_contract import AccessScope
from .g3_projection_feed import _binding
from .memory_auth_epoch_store import AuthenticatedLogin, ServerIssuedEpoch, AuthEpochRegistry, AuthEpochError
from .session_lifecycle import _session_id
from .session_lifecycle_codec import checksum, encode_record
from .session_lifecycle_coordination import SharedSessionCoordinator, encode
from .session_lifecycle_status_proxy import TrustedStatusContext
from .session_lifecycle_storage import ConfigView, _selected, _owned, _Transaction
from .web_session_provider_authority import WebSessionProviderAuthority


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedRecoveryLogin:
    """Only a trusted host login adapter may supply these; type is not proof."""
    identity: AuthenticatedLogin
    epoch: ServerIssuedEpoch


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryStatusBinding:
    """Server result. Only identity is suitable for future browser binding.

    identity is not a credential or grant; context stays server-side. No HTTP
    endpoint or browser publication is provided by this module.
    """
    identity: str
    context: TrustedStatusContext


@dataclass(frozen=True, slots=True, repr=False)
class _CanonicalSnapshot:
    access: AccessScope
    generation: int
    conversation_id: str
    provider: str
    version_id: str
    record_sha256: str


class RecoveryStatusContextProvider:
    production_supported = False

    def __init__(self, *, enabled=False, coordinator=None, epoch_path=None,
                 login_provider=None, authorize=None, historical_provider=None,
                 identity_key=None, connection_namespace=None):
        self._enabled = enabled is True
        if not self._enabled:
            return
        try:
            path = Path(epoch_path)
            if (type(coordinator) is not SharedSessionCoordinator or not coordinator.enabled
                    or not path.is_absolute() or '..' in path.parts
                    or coordinator.canonical_path is None
                    or Path(coordinator.canonical_path) != path
                    or not all(callable(fn) for fn in (login_provider, authorize, historical_provider))
                    or inspect.iscoroutinefunction(authorize) or inspect.iscoroutinefunction(historical_provider)
                    or type(identity_key) is not bytes or len(identity_key) != 32
                    or type(connection_namespace) is not str or not 1 <= len(connection_namespace) <= 128
                    or any(ord(c) < 33 or ord(c) > 126 for c in connection_namespace)):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('lifecycle_status_context_binding_invalid') from None
        self._coordinator, self._path = coordinator, path
        self._login_provider, self._authorize, self._history = login_provider, authorize, historical_provider
        self._key, self._namespace = identity_key, connection_namespace

    def _mac(self, purpose, values):
        return hmac.new(self._key, encode([purpose, self._namespace, *values]), hashlib.sha256).hexdigest()

    async def _login(self, request):
        value = self._login_provider(request)
        if inspect.isawaitable(value):
            value = await value
        if (type(value) is not AuthenticatedRecoveryLogin
                or type(value.identity) is not AuthenticatedLogin or type(value.epoch) is not ServerIssuedEpoch):
            raise HTTPException(503)
        return value

    def _registry(self, login):
        # Request-owned immutable capture; no process-global current user.
        return AuthEpochRegistry(path=self._path, identity_provider=lambda: login.identity)

    def _grant(self, login, cid):
        value = self._authorize(login.identity, cid)
        if (type(value) is not AccessScope or value.principal_id != login.identity.principal_id):
            raise HTTPException(403)
        _binding(value)
        return value

    def _capture(self, login, cid):
        registry = self._registry(login)
        access = self._grant(login, cid); expected = _binding(access)
        observed = []
        def guard():
            for sid in (cid, *observed):
                if _binding(self._grant(login, sid)) != expected:
                    raise HTTPException(403)
        def read(legacy, generation, conn):
            registry.assert_current_in_read_transaction(conn, login.epoch)
            config = legacy.load_config()
            rows = config.get('sessions'); tombstones = config.get('deleted_sessions', [])
            # Preserve WebSessionProviderAuthority's existing None-as-empty
            # tombstone contract without rewriting the canonical JSON.
            if tombstones is None:
                tombstones = []
            if (type(rows) is not list or len(rows) > 512
                    or type(tombstones) is not list or len(tombstones) > 512):
                raise HTTPException(503)
            authority = WebSessionProviderAuthority(legacy, historical_provider=self._history)
            live = authority.session_rows()
            row = next((row for row in live if row['id'] == cid), None)
            if row is None or authority.tombstone_for_session(cid) is not None:
                raise HTTPException(403)
            view = ConfigView(cid, row['provider'], tuple((r['id'], r['provider']) for r in live), ())
            version = _selected(conn, view)
            observed[:] = [] if version == cid else [version]
            guard()
            _owned(conn)
            tx = _Transaction(conn, view, readonly=True)
            try:
                state = tx.load()
            finally:
                tx.active = False
            if state is None:
                raise HTTPException(503)
            if state.access_binding != expected:
                raise HTTPException(403)
            # The current internal status journal supports API only. A valid
            # Codex login is not evidence that its status path is implemented.
            if row['provider'] != 'api' or state.provider != 'api':
                raise HTTPException(503)
            registry.assert_current_in_read_transaction(conn, login.epoch)
            return _CanonicalSnapshot(access, generation, cid, row['provider'], version,
                                      checksum(encode_record(state)))
        return self._coordinator.observe_canonical(read, guard=guard)

    async def binding(self, request, conversation_id):
        if not self._enabled:
            raise HTTPException(503)
        try:
            if not isinstance(request, Request) or not _session_id(conversation_id):
                raise HTTPException(400)
            first = await self._login(request)
            registry = self._registry(first)
            await asyncio.to_thread(registry.assert_current, first.epoch)
            before = await asyncio.to_thread(self._capture, first, conversation_id)
            current = await self._login(request)
            if current != first:
                raise HTTPException(403)
            await asyncio.to_thread(registry.assert_current, first.epoch)
            after = await asyncio.to_thread(self._capture, first, conversation_id)
            if before != after:
                raise HTTPException(403)
            if await self._login(request) != first:
                raise HTTPException(403)
            # A WAL snapshot cannot observe a logout committed by another
            # worker after that snapshot began.
            await asyncio.to_thread(registry.assert_current, first.epoch)
            access_binding = _binding(after.access)
            identity = 'recovery:' + self._mac('status-identity-v1', [first.identity.principal_id])
            auth_epoch = 'auth:' + self._mac('status-auth-v1', [first.identity.principal_id,
                first.identity.login_id, first.epoch.value, access_binding])
            version = 'ctx:' + self._mac('status-context-v1', [conversation_id, after.generation,
                after.provider, after.version_id, after.record_sha256, access_binding])
            return RecoveryStatusBinding(identity, TrustedStatusContext(after.access,
                conversation_id, auth_epoch, version))
        except HTTPException as error:
            code = error.status_code if error.status_code in (400, 401, 403) else 503
            raise HTTPException(code, 'status_context_unavailable') from None
        except AuthEpochError:
            # The existing epoch API intentionally has one denial category for
            # revoked/rotated/missing/unavailable registry states. Fail closed.
            raise HTTPException(403, 'status_context_unavailable') from None
        except Exception:
            raise HTTPException(503, 'status_context_unavailable') from None

    async def authenticate(self, request, conversation_id):
        return (await self.binding(request, conversation_id)).context
