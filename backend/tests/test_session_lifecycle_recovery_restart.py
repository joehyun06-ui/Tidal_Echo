"""GM-02AS rehearsal against a disposable canonical database and WAL."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx

from backend import channel_store
from backend.memory_auth_epoch_store import AuthenticatedLogin, AuthEpochRegistry, ServerIssuedEpoch
from backend.session_lifecycle_coordination import SharedSessionCoordinator
from backend.session_lifecycle_recovery_binding_http import PATH as BINDING_PATH, install as install_binding
from backend.session_lifecycle_status_context import AuthenticatedRecoveryLogin, RecoveryStatusContextProvider
from backend.session_lifecycle_status_proxy import PATH as STATUS_PATH, install as install_proxy
from backend.session_lifecycle_storage import JsonSessionReader, SQLiteLifecycleRepository
from backend.session_window_activation_http import install as install_internal
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempts import WindowAttemptJournal
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_activation_http import fixture
from backend.tests.test_session_lifecycle_coordination import validator
from backend.tests.test_session_lifecycle_history_http import TOKEN, original_auth
from backend.tests.test_session_lifecycle_status_context import BROWSER, KEY, ORIGIN


LOGIN = AuthenticatedLogin(CTX.principal_id, 'L' * 32)


def resources(path, *, readonly):
    def connect():
        if readonly:
            return channel_store.connect_read_only(path, timeout_seconds=3)
        return channel_store.connect(path)

    coordinator = SharedSessionCoordinator(
        connect, path.with_suffix('.json'), path.parent / 'gate',
        validate=validator, enabled=True, canonical_path=path,
    )
    return connect, coordinator


async def observe(path, data):
    connect, coordinator = resources(path, readonly=True)
    history = lambda _: 'api'
    reader = JsonSessionReader(path.with_suffix('.json'), historical_provider=history)
    repository = SQLiteLifecycleRepository(connect, reader, enabled=True,
                                           coordinator=coordinator)
    journal = WindowAttemptJournal(repository, enabled=True)
    login = AuthenticatedRecoveryLogin(LOGIN, ServerIssuedEpoch(data['epoch']))

    def authenticate(request):
        if request.headers.getlist('authorization') != [BROWSER]:
            raise HTTPException(401)
        return login

    def authorize(identity, cid):
        return CTX if identity == LOGIN and cid == CID else None

    provider = RecoveryStatusContextProvider(
        enabled=True, coordinator=coordinator, epoch_path=path,
        login_provider=authenticate, authorize=authorize, historical_provider=history,
        identity_key=KEY, connection_namespace='synthetic-restart',
    )
    app, internal = FastAPI(), FastAPI()
    check = original_auth()

    def internal_auth(request):
        check(request)
        return CTX

    install_internal(internal, journal, authenticate=internal_auth, enabled=True)
    install_binding(app, enabled=True, provider=provider, expected_access=CTX,
                    expected_origin=ORIGIN)
    forwarded = []

    def factory(**options):
        async def record(request):
            forwarded.append(request.url.path)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal),
            event_hooks={'request': [record]},
            **options,
        )

    install_proxy(
        app, enabled=True, authenticate=provider.authenticate, expected_access=CTX,
        expected_origin=ORIGIN, base_url='http://127.0.0.1:3020',
        internal_token=TOKEN, client_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='https://relay.synthetic.invalid',
        headers={'Origin': ORIGIN, 'Authorization': BROWSER},
    ) as client:
        binding = await client.post(BINDING_PATH.replace('{conversation_id}', CID))
        status = await client.post(STATUS_PATH.replace('{conversation_id}', CID), json=data['reference'])
    return {
        'binding': [binding.status_code, binding.json()],
        'status': [status.status_code, status.json()],
        'forwarded': len(forwarded),
    }


def worker(path):
    data = json.load(sys.stdin)
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')), \
            patch('socket.getaddrinfo', side_effect=AssertionError('offline')):
        print(json.dumps(asyncio.run(observe(Path(path), data))))


@unittest.skipUnless(os.name == 'posix', 'canonical coordinator uses POSIX locks')
class RecoveryRestartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'canonical.sqlite'
        self.h, journal, self.reference = fixture(self.path)
        self.addCleanup(self.h.conn.close)
        self.enterContext(patch.dict(os.environ, {'MEMORY_TRUSTED_AUTH_EPOCH_ENABLED': 'true'}))
        with closing(channel_store.connect(self.path)) as database:
            self.assertEqual(database.execute('PRAGMA journal_mode=WAL').fetchall()[0][0], 'wal')
            self.assertEqual(database.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()[0][0], 0)
        (self.path.parent / 'gate').mkdir(mode=0o700)
        _, coordinator = resources(self.path, readonly=False)
        coordinator.bootstrap()
        self.registry = AuthEpochRegistry(path=self.path, identity_provider=lambda: LOGIN)
        self.epoch = self.registry.issue_after_authenticated_login()
        state = self.h.record()
        journal.record_attempt(
            CID, self.reference['attempt_request_id'], packet_sha256=state.packet_sha256,
            expected_revision=state.revision, access=CTX,
        )
        self.h.confirm()
        activate_recorded_attempt(
            self.h.engine, journal, CID, self.reference['activation_request_id'],
            attempt_request_id=self.reference['attempt_request_id'],
            packet_sha256=state.packet_sha256, access=CTX, enabled=True,
        )
        self.revision = self.h.record().revision

    def observe_new_process(self):
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; from backend.tests.test_session_lifecycle_recovery_restart '
             'import worker; worker(sys.argv[1])', str(self.path)],
            input=json.dumps({'epoch': self.epoch.value, 'reference': self.reference}),
            capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(result.stdout)

    def assert_historical(self, result):
        self.assertEqual(result['binding'][0], 200, result)
        self.assertEqual(result['binding'][1]['conversationId'], CID)
        self.assertEqual(result['status'][0], 200, result)
        status = result['status'][1]
        self.assertEqual(status['state'], 'historical_confirmed')
        self.assertEqual(status['revision_applied'], self.revision)
        for key in ('current_window_confirmed', 'reply_completed', 'retry_authorized'):
            self.assertIs(status[key], False)
        self.assertEqual(result['forwarded'], 1)

    def test_restart_preserves_binding_and_historical_receipt_then_refuses_revoked_epoch(self):
        rows = list(self.h.conn.iterdump())
        config = self.path.with_suffix('.json').read_bytes()
        first = self.observe_new_process()
        self.assert_historical(first)
        self.assertEqual(self.observe_new_process(), first)
        self.assertEqual(list(self.h.conn.iterdump()), rows)
        self.assertEqual(self.path.with_suffix('.json').read_bytes(), config)

        self.assertTrue(self.registry.revoke_before_logout())
        revoked = self.observe_new_process()
        self.assertEqual(revoked['binding'], [403, {'ok': False, 'error': 'access_denied'}])
        self.assertEqual(revoked['status'], revoked['binding'])
        self.assertEqual(revoked['forwarded'], 0)

    def test_online_backup_restores_wal_receipt_at_original_canonical_path(self):
        rows = list(self.h.conn.iterdump())
        config_path = self.path.with_suffix('.json')
        config = config_path.read_bytes()
        original = self.observe_new_process()
        self.assert_historical(original)
        # Confirm that copying only the live main file would miss the commits.
        with closing(sqlite3.connect(self.path.as_uri() + '?immutable=1', uri=True)) as base:
            self.assertEqual(base.execute('SELECT count(*) FROM session_window_attempts').fetchone()[0], 0)
        backup = self.path.with_name('backup.sqlite')
        with closing(sqlite3.connect(backup)) as destination:
            self.h.conn.backup(destination)
            self.assertEqual(destination.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(list(destination.iterdump()), rows)
        self.h.conn.close()
        self.assertFalse(self.path.with_name(self.path.name + '-wal').exists())
        self.path.rename(self.path.with_name('before-restore.sqlite'))
        backup.rename(self.path)

        restored = self.observe_new_process()
        self.assertEqual(restored, original)
        with closing(channel_store.connect_read_only(self.path, timeout_seconds=3)) as database:
            self.assertEqual(list(database.iterdump()), rows)
        self.assertEqual(config_path.read_bytes(), config)
