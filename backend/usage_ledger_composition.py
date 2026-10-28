"""Explicit ledger services for offline composition acceptance.

No environment switch, singleton, worker, RPC, live-entrypoint import or mount.
Each process constructs its own services over the same dedicated SQLite file.
Runtime kwargs must be passed at construction; existing runtimes are never patched.
"""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3

from .usage_ledger import Ledger, LedgerError, identifier
from .usage_ledger_capture import ApiCollector, RouteProfile
from .usage_ledger_codex import CodexCollector
from .usage_ledger_compaction import ManualCompactionReceipts
from .usage_ledger_continuation import Continuations
from .usage_ledger_creation import ContinuationCreation
from .usage_ledger_preflight import CODEX_SCHEMA_VERSION
from .usage_ledger_paths import separate_paths
from .usage_ledger_sources import MessageCounts


def invalid():
    raise LedgerError('ledger_composition_invalid')


class LedgerServices:
    def __init__(self, *, ledger_path, relay_db, continuation_key,
                 native_db=None, api_profiles=None, fingerprint_key=None,
                 codex_provider_id=None, codex_cli_version=None,
                 exclusive_threads=False, max_pending=32):
        # Validate all config before creating any ledger tables/journal runs.
        try:
            paths = separate_paths(ledger_path, relay_db, native_db)
            if not isinstance(continuation_key, bytes) or len(continuation_key) < 32: invalid()
            if type(exclusive_threads) is not bool or type(max_pending) is not int or not 1 <= max_pending <= 128: invalid()
            profiles = None
            if api_profiles is not None:
                if not isinstance(api_profiles, (list, tuple)) or not 1 <= len(api_profiles) <= 128: invalid()
                if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32: invalid()
                profiles = []
                keys = set()
                for profile in api_profiles:
                    if not isinstance(profile, RouteProfile): invalid()
                    copy = RouteProfile(**profile.__dict__)
                    key = (copy.endpoint.rstrip('/'), copy.model, copy.protocol)
                    if key in keys: invalid()
                    keys.add(key); profiles.append(copy)
            elif fingerprint_key is not None:
                invalid()
            if codex_provider_id is not None:
                identifier(codex_provider_id)
                if paths[2] is None or codex_cli_version != CODEX_SCHEMA_VERSION: invalid()
            elif codex_cli_version is not None or exclusive_threads:
                invalid()
        except (ValueError, TypeError, OSError, RuntimeError):
            raise LedgerError('ledger_composition_invalid') from None

        self.relay_db, self.native_db = paths[1:]
        self._authority = self._lock = None
        self.creator = None
        try:
            self.ledger = Ledger(paths[0])
            self.api = ApiCollector(self.ledger, profiles, fingerprint_key=fingerprint_key) if profiles is not None else None
            self.codex = CodexCollector(self.ledger, relay_db=self.relay_db, store_path=self.native_db,
                                       provider_id=codex_provider_id, exclusive_threads=exclusive_threads,
                                       max_pending=max_pending) if codex_provider_id is not None else None
            self.messages = MessageCounts(self.relay_db, session_row=self._row)
            self.continuations = Continuations(self.ledger, relay_db=self.relay_db, native_db=self.native_db,
                                              session_row=self._row, digest_key=continuation_key)
            self.manual_compactions = ManualCompactionReceipts(self.ledger, native_db=self.native_db,
                                          relay_db=self.relay_db, session_row=self._row) if self.native_db is not None else None
        except (ValueError, TypeError, OSError, sqlite3.Error):
            raise LedgerError('ledger_composition_unavailable') from None

    def _row(self, sid):
        return self._authority.row_for_session(sid) if self._authority is not None else None

    def bind_authority(self, authority, session_lock, *, create_session=None):
        """One authoritative resolver, shared creation/deletion lock, optional factory.

        Bind after the native runtime/integration exists and before serving requests.
        Before binding, source callbacks report unavailable instead of guessing.
        """
        if self._authority is not None: raise LedgerError('ledger_composition_already_bound')
        if (not callable(getattr(authority, 'row_for_session', None))
                or not hasattr(session_lock, '__enter__') or not hasattr(session_lock, '__exit__')
                or create_session is not None and not callable(create_session)):
            invalid()
        try:
            source = Path(authority.legacy.RELAY_DB).resolve()
            if source != self.relay_db and not source.samefile(self.relay_db): invalid()
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            raise LedgerError('ledger_composition_invalid') from None
        try:
            creator = ContinuationCreation(self.continuations, authority=authority, session_lock=session_lock,
                                           create_session=create_session) if create_session is not None else None
        except (ValueError, TypeError, OSError, sqlite3.Error):
            raise LedgerError('ledger_composition_unavailable') from None
        self._authority, self._lock, self.creator = authority, session_lock, creator

    def _bound(self):
        if self._authority is None: raise LedgerError('ledger_composition_unbound')

    def api_runtime_kwargs(self):
        return {'usage_collector': self.api} if self.api is not None else {}

    def codex_runtime_kwargs(self):
        return {'usage_collector': self.codex, 'compaction_collector': self.manual_compactions} if self.codex is not None else {}

    def action_kwargs(self):
        self._bound()
        if self.creator is None: raise LedgerError('ledger_composition_factory_unavailable')
        return {'continuation_creator': self.creator}

    def deletion_kwargs(self):
        """For the existing version-delete installer; caller supplies the same authority/lock."""
        self._bound()
        return {'usage_ledger': self.ledger}

    def delete_session(self, sid, *, upload_dir=None):
        self._bound()
        from .web_session_delete import delete_conversation
        with self._lock:
            return delete_conversation(self._authority, sid, relay_db=self.relay_db,
                                       codex_store=self.native_db, upload_dir=upload_dir, usage_ledger=self.ledger)

    def install_reader(self, app, *, check_auth, prefix='/app'):
        self._bound()
        if not callable(check_auth) or prefix not in {'/app','/loop'}: invalid()
        paths = {prefix+p for p in ('/sessions/{}/usage-ledger', '/sessions/{}/usage-ledger/export',
                                   '/sessions/{}/usage-ledger/records', '/usage-ledger/settings/{}')}
        # Reject differently named path parameters too: they match the same URLs.
        if any(re.sub(r'\{[^}]+\}', '{}', getattr(r, 'path', '')) in paths for r in app.routes):
            raise LedgerError('ledger_composition_route_conflict')
        from .usage_ledger_routes import install
        install(app, ledger=self.ledger, check_auth=check_auth,
                session_provider=lambda sid: (self._row(sid) or {}).get('provider'),
                message_source=self.messages, continuations=self.continuations,
                manual_compactions=self.manual_compactions, prefix=prefix)
