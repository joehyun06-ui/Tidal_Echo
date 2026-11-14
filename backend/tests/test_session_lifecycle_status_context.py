"""Real durable epoch/canonical DB reads and AR gateway; synthetic login only."""
import asyncio
from dataclasses import replace
import inspect
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx
from starlette.requests import Request

from backend.g3_projection_contract import AccessScope
from backend.memory_auth_epoch_store import AuthenticatedLogin, ServerIssuedEpoch, AuthEpochRegistry, AuthEpochError
from backend.session_lifecycle_codec import encode_record, checksum
from backend import session_lifecycle_recovery_binding_http as recovery_binding
from backend.session_lifecycle_status_context import AuthenticatedRecoveryLogin, RecoveryStatusContextProvider
from backend.session_lifecycle_status_proxy import install, PATH
from backend.session_window_activation_http import install as install_internal
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_activation_http import fixture
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_history_http import TOKEN, original_auth

KEY = b'synthetic-identity-key-material!'
BROWSER = 'Bearer synthetic-login-credential'
ORIGIN = 'https://frontend.synthetic.invalid'


def request(headers=None):
    pairs = {'authorization': BROWSER, **(headers or {})}
    return Request({'type':'http','method':'POST','scheme':'https','server':('synthetic.invalid',443),
        'path':PATH.replace('{conversation_id}',CID),'query_string':b'',
        'headers':[(key.encode(),value.encode()) for key,value in pairs.items()]})


@unittest.skipUnless(os.name == 'posix', 'existing canonical coordinator/private JSON fixture is POSIX')
class StatusContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.h,self.journal,self.body = fixture(self.root/'canonical.sqlite'); self.addCleanup(self.h.conn.close)
        (self.root/'gate').mkdir(mode=0o700)
        self.c = coordinator(self.h.path); self.c.canonical_path = Path(self.h.path); self.c.bootstrap()
        self.enterContext(patch.dict(os.environ, {'MEMORY_TRUSTED_AUTH_EPOCH_ENABLED':'true'}))
        self.login_identity = AuthenticatedLogin(CTX.principal_id,'L'*32)
        self.registry = AuthEpochRegistry(path=Path(self.h.path),identity_provider=lambda:self.login_identity)
        self.login = AuthenticatedRecoveryLogin(self.login_identity,self.registry.issue_after_authenticated_login())
        self.allowed = {CID}; self.scope = CTX; self.login_calls = 0; self.grant_calls = 0
        self.login_hook = None; self.grant_hook = None
        self.history_value = 'api'
        self.provider = self.build()
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline only')))

    async def login_provider(self, incoming):
        self.login_calls += 1
        if incoming.headers.getlist('authorization') != [BROWSER]: raise HTTPException(401)
        if self.login_hook:
            value = self.login_hook(self.login_calls)
            if inspect.isawaitable(value): await value
        return self.login

    def authorize(self, identity, cid):
        self.grant_calls += 1
        if self.grant_hook: self.grant_hook(self.grant_calls)
        if identity.principal_id != self.scope.principal_id or cid not in self.allowed:
            raise HTTPException(403)
        return self.scope

    def build(self, **changes):
        return RecoveryStatusContextProvider(**{**dict(enabled=True,coordinator=self.c,
            epoch_path=Path(self.h.path),login_provider=self.login_provider,authorize=self.authorize,
            historical_provider=lambda _:self.history_value,identity_key=KEY,
            connection_namespace='synthetic-connection-1'),**changes})

    async def denied(self, function, code):
        with self.assertRaises(HTTPException) as caught: await function()
        self.assertEqual(caught.exception.status_code,code)
        self.assertEqual(caught.exception.detail,'status_context_unavailable')

    def patch_config(self, request_id, **values):
        def change(legacy):
            config=legacy.load_config();config['sessions'][0].update(values);legacy.save_config(config)
        return self.c.mutate(request_id,values,change)

    def save_record(self, state):
        data=encode_record(state)
        self.h.conn.execute('UPDATE session_lifecycle_records SET revision=?,payload=?,sha256=? WHERE conversation_id=?',
                            (state.revision,data,checksum(data),CID))

    async def test_default_off_inspects_no_dependencies_and_creates_no_files(self):
        provider=RecoveryStatusContextProvider(epoch_path=object(),coordinator=object(),identity_key=object())
        with self.assertRaises(HTTPException) as caught: await provider.binding(None,None)
        self.assertEqual(caught.exception.status_code,503)
        self.assertEqual(self.login_calls,0);self.assertEqual(self.grant_calls,0)

    async def test_enabled_configuration_requires_pinned_existing_resources(self):
        for change in ({'identity_key':b'short'},{'connection_namespace':''},{'connection_namespace':'x\ny'},
            {'epoch_path':self.root/'other.sqlite'},{'login_provider':None},
            {'authorize':None},{'historical_provider':None},{'coordinator':object()}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.build(**change)
        self.assertFalse((self.root/'other.sqlite').exists())
        self.c.canonical_path=None
        with self.assertRaises(ValueError):self.build()

    async def test_binding_is_stable_noncredential_and_read_only(self):
        before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        first=await self.provider.binding(request(),CID);second=await self.build().binding(request(),CID)
        self.assertEqual(first,second);self.assertTrue(first.identity.startswith('recovery:'))
        for text in (first.identity,first.context.auth_epoch,first.context.context_version,repr(first),repr(first.context)):
            self.assertNotIn(self.login.epoch.value,text);self.assertNotIn(BROWSER,text)
        self.assertNotIn(CTX.principal_id,first.identity)
        self.assertEqual(first.context.access,CTX);self.assertEqual(first.context.conversation_id,CID)
        self.assertEqual(before,list(self.h.conn.iterdump()));self.assertEqual(config,self.h.config_path.read_bytes())

    async def test_relogin_rotates_auth_label_but_keeps_identity_and_context(self):
        first=await self.provider.binding(request(),CID)
        self.login=replace(self.login,epoch=self.registry.issue_after_authenticated_login())
        second=await self.provider.binding(request(),CID)
        self.assertEqual(first.identity,second.identity)
        self.assertNotEqual(first.context.auth_epoch,second.context.auth_epoch)
        self.assertEqual(first.context.context_version,second.context.context_version)

    async def test_namespace_and_key_rotation_separate_local_reference_identity(self):
        original=await self.provider.binding(request(),CID)
        for change in ({'connection_namespace':'other-connection'},{'identity_key':b'k'*32}):
            other=await self.build(**change).binding(request(),CID)
            self.assertNotEqual(original.identity,other.identity)

    async def test_unauthorized_login_precedes_epoch_and_canonical_reads(self):
        with patch.object(AuthEpochRegistry,'assert_current',side_effect=AssertionError('no registry IO')):
            await self.denied(lambda:self.provider.binding(request({'authorization':'wrong'}),CID),401)
        self.assertEqual(self.grant_calls,0)

    async def test_shared_secret_none_review_or_raw_epoch_cannot_be_promoted(self):
        for value in (None,True,CTX,self.login.identity,
                replace(self.login,epoch=self.login.epoch.value),
                replace(self.login,identity={'principal_id':CTX.principal_id})):
            self.login=value
            await self.denied(lambda:self.provider.binding(request(),CID),503)
        self.assertEqual(self.grant_calls,0)

    async def test_missing_rotated_or_logged_out_epoch_denies_before_canonical(self):
        self.registry.issue_after_authenticated_login()
        await self.denied(lambda:self.provider.binding(request(),CID),403)
        self.login=replace(self.login,epoch=self.registry.issue_after_authenticated_login())
        other_worker=AuthEpochRegistry(path=Path(self.h.path),identity_provider=lambda:self.login_identity)
        self.assertTrue(other_worker.revoke_before_logout())
        await self.denied(lambda:self.build().binding(request(),CID),403)
        self.assertEqual(self.grant_calls,0)

    async def test_missing_epoch_schema_is_not_recreated(self):
        self.h.conn.execute('DROP TABLE memory_auth_epoch_v1')
        await self.denied(lambda:self.provider.binding(request(),CID),403)
        self.assertIsNone(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_auth_epoch_v1'").fetchone())

    async def test_epoch_feature_disabled_does_not_enable_itself(self):
        with patch.dict(os.environ,{'MEMORY_TRUSTED_AUTH_EPOCH_ENABLED':'false'}):
            await self.denied(lambda:self.provider.binding(request(),CID),403)
            self.assertEqual(os.environ['MEMORY_TRUSTED_AUTH_EPOCH_ENABLED'],'false')
        self.assertEqual(self.grant_calls,0)

    async def test_forged_browser_identity_fields_are_not_trusted(self):
        binding=await self.provider.binding(request({'x-principal-id':'admin','x-auth-epoch':'forged',
            'x-session-id':'other','x-login-id':'fake'}),CID)
        self.assertEqual(binding.context.access,CTX)
        self.assertNotIn('admin',binding.identity);self.assertNotIn('forged',binding.context.auth_epoch)

    async def test_actual_record_owner_binding_cannot_be_bypassed_by_scope_callback(self):
        self.login_identity=AuthenticatedLogin('other_owner','M'*32)
        self.login=AuthenticatedRecoveryLogin(self.login_identity,self.registry.issue_after_authenticated_login())
        self.scope=AccessScope('other_owner',CTX.allowed_scopes)
        await self.denied(lambda:self.provider.binding(request(),CID),403)

    async def test_missing_or_deleted_root_cannot_be_authorized_by_id_alone(self):
        self.allowed.add('other_chat')
        await self.denied(lambda:self.provider.binding(request(),'other_chat'),403)
        def remove(legacy):
            config=legacy.load_config();config['sessions']=[];config['deleted_sessions']=[{
                'id':CID,'provider':'api','deleted_at':'2026-11-10T00:00:00Z'}];legacy.save_config(config)
        self.c.mutate('delete-root',{},remove)
        await self.denied(lambda:self.provider.binding(request(),CID),403)

    async def test_existing_null_tombstone_representation_remains_read_only(self):
        def legacy_null(legacy):
            config=legacy.load_config();config['deleted_sessions']=None;legacy.save_config(config)
        self.c.mutate('legacy-null-tombstones',{},legacy_null)
        before=list(self.h.conn.iterdump());raw=self.h.config_path.read_bytes()
        binding=await self.provider.binding(request(),CID)
        self.assertEqual(binding.context.access,CTX)
        self.assertEqual(raw,self.h.config_path.read_bytes());self.assertEqual(before,list(self.h.conn.iterdump()))

    async def test_selected_version_requires_its_own_grant(self):
        def version(legacy):
            config=legacy.load_config();row=dict(config['sessions'][0]);row['id']='version_2';
            config['sessions'].append(row);legacy.save_config(config)
        self.c.mutate('publish-version',{},version)
        self.h.conn.execute("INSERT INTO web_message_versions VALUES ('version_2',?,?,'edit',1,1,1)",(CID,CID))
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,'version_2'))
        await self.denied(lambda:self.provider.binding(request(),CID),403)

    async def test_coordination_generation_detects_identical_config_aba(self):
        self.patch_config('normalize-pin',pinned=False)
        before=await self.provider.binding(request(),CID);raw=self.h.config_path.read_bytes()
        self.patch_config('pin-on',pinned=True);self.patch_config('pin-off',pinned=False)
        self.assertEqual(raw,self.h.config_path.read_bytes())
        after=await self.provider.binding(request(),CID)
        self.assertEqual(before.identity,after.identity)
        self.assertNotEqual(before.context.context_version,after.context.context_version)

    async def test_canonical_aba_during_login_resample_refuses_stale_binding(self):
        self.patch_config('normalize-pin',pinned=False);before=self.h.config_path.read_bytes()
        def change(count):
            if count==2:
                self.patch_config('mid-on',pinned=True);self.patch_config('mid-off',pinned=False)
        self.login_hook=change
        await self.denied(lambda:self.provider.binding(request(),CID),403)
        self.assertEqual(before,self.h.config_path.read_bytes())

    async def test_lifecycle_revision_changes_context_without_changing_login_identity(self):
        before=await self.provider.binding(request(),CID);state=self.h.record()
        self.h.engine.reentry_delta(CID,'context-delta',after_source=state.source_cursor,
                                   after_message=state.message_cursor,access=CTX)
        after=await self.provider.binding(request(),CID)
        self.assertEqual(before.identity,after.identity);self.assertEqual(before.context.auth_epoch,after.context.auth_epoch)
        self.assertNotEqual(before.context.context_version,after.context.context_version)

    async def test_epoch_rotation_during_lookup_rejects_old_authenticated_snapshot(self):
        def rotate(count):
            if count==2:self.login=replace(self.login,epoch=self.registry.issue_after_authenticated_login())
        self.login_hook=rotate
        await self.denied(lambda:self.provider.binding(request(),CID),403)

    async def test_logout_during_final_wal_snapshot_refuses_binding(self):
        self.assertEqual(self.h.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        original_capture = self.provider._capture
        captures = 0
        revoked = []

        def capture(login, cid):
            nonlocal captures
            captures += 1
            if captures == 2:
                def logout_during_read(_):
                    # The historical provider runs after the canonical read
                    # transaction is pinned. WAL lets another worker log out.
                    if not revoked:
                        revoked.append(self.registry.revoke_before_logout())
                    return 'api'
                with patch.object(self.provider, '_history', logout_during_read):
                    return original_capture(login, cid)
            return original_capture(login, cid)

        with patch.object(self.provider, '_capture', capture):
            await self.denied(lambda: self.provider.binding(request(), CID), 403)
        self.assertEqual(revoked, [True])

    async def test_host_login_expiring_during_final_snapshot_refuses_binding(self):
        now = 0
        expires_at = 1
        original_capture = self.provider._capture
        captures = 0

        async def authenticate(incoming):
            if now >= expires_at:
                raise HTTPException(401)
            return await self.login_provider(incoming)

        def capture(login, cid):
            nonlocal captures, now
            snapshot = original_capture(login, cid)
            captures += 1
            if captures == 2:
                now = expires_at
            return snapshot

        provider = self.build(login_provider=authenticate)
        with patch.object(provider, '_capture', capture):
            await self.denied(lambda: provider.binding(request(), CID), 401)
        self.assertEqual(captures, 2)
        self.registry.assert_current(self.login.epoch)

    async def test_grant_revocation_during_canonical_projection_refuses_result(self):
        def revoke(count):
            if count==3:self.allowed.clear()
        self.grant_hook=revoke
        await self.denied(lambda:self.provider.binding(request(),CID),403)

    async def test_unknown_canonical_generation_and_missing_records_fail_closed(self):
        self.h.conn.execute('DELETE FROM session_lifecycle_receipts')
        self.h.conn.execute('DELETE FROM session_lifecycle_records')
        await self.denied(lambda:self.provider.binding(request(),CID),503)
        self.h.conn.execute('DROP TABLE session_coordination_meta')
        await self.denied(lambda:self.provider.binding(request(),CID),503)

    async def test_uncooperative_json_change_is_not_repaired(self):
        from backend.deployment_config import atomic_write_text
        atomic_write_text(self.h.config_path,self.h.config_path.read_text()+' ')
        before=self.h.config_path.read_bytes();rows=list(self.h.conn.iterdump())
        await self.denied(lambda:self.provider.binding(request(),CID),503)
        self.assertEqual(before,self.h.config_path.read_bytes());self.assertEqual(rows,list(self.h.conn.iterdump()))

    async def test_provider_and_login_errors_are_redacted(self):
        async def fail(_):raise RuntimeError('raw private server details')
        await self.denied(lambda:self.build(login_provider=fail).binding(request(),CID),503)
        await self.denied(lambda:self.build(historical_provider=lambda _:object()).binding(request(),CID),503)

    async def test_codex_record_is_explicitly_unsupported_by_api_status_journal(self):
        self.patch_config('use-codex',provider='codex');self.history_value='codex'
        self.save_record(replace(self.h.record(),provider='codex'))
        await self.denied(lambda:self.provider.binding(request(),CID),503)

    async def test_two_concurrent_logins_never_share_captured_epoch(self):
        first=self.login
        self.login_identity=AuthenticatedLogin(CTX.principal_id,'M'*32)
        second=AuthenticatedRecoveryLogin(self.login_identity,self.registry.issue_after_authenticated_login())
        async def pick(incoming):
            await asyncio.sleep(0)
            return first if incoming.headers['authorization']=='first' else second
        provider=self.build(login_provider=pick)
        one,two=await asyncio.gather(provider.binding(request({'authorization':'first'}),CID),
                                     provider.binding(request({'authorization':'second'}),CID))
        self.assertEqual(one.identity,two.identity)
        self.assertNotEqual(one.context.auth_epoch,two.context.auth_epoch)
        self.assertEqual(one.context.context_version,two.context.context_version)

    async def test_new_read_transaction_epoch_api_requires_same_query_only_main_db(self):
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current_in_read_transaction(self.h.conn,self.login.epoch)
        self.h.conn.execute('BEGIN')
        try:
            with self.assertRaises(AuthEpochError):
                self.registry.assert_current_in_read_transaction(self.h.conn,self.login.epoch)
        finally:self.h.conn.rollback()
        self.h.conn.execute('PRAGMA query_only=ON');self.h.conn.execute('BEGIN')
        try:self.registry.assert_current_in_read_transaction(self.h.conn,self.login.epoch)
        finally:self.h.conn.rollback();self.h.conn.execute('PRAGMA query_only=OFF')
        with sqlite3.connect(self.root/'other.sqlite',isolation_level=None) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            with self.assertRaises(AuthEpochError):
                self.registry.assert_current_in_read_transaction(conn,self.login.epoch)
            conn.rollback()

    async def test_shared_epoch_query_does_not_fall_back_to_an_attached_database(self):
        other=self.root/'attached.sqlite'
        with sqlite3.connect(other) as conn:
            conn.execute('CREATE TABLE memory_auth_epoch_v1 (principal_id TEXT,login_id TEXT,epoch_sha256 TEXT,revoked INTEGER)')
            conn.execute('INSERT INTO memory_auth_epoch_v1 VALUES (?,?,?,0)',(self.login_identity.principal_id,
                self.login_identity.login_id,hashlib.sha256(self.login.epoch.value.encode('ascii')).hexdigest()))
        self.h.conn.execute('ATTACH DATABASE ? AS extra',(str(other),))
        self.h.conn.execute('DROP TABLE main.memory_auth_epoch_v1')
        self.h.conn.execute('PRAGMA query_only=ON');self.h.conn.execute('BEGIN')
        try:
            with self.assertRaises(AuthEpochError):
                self.registry.assert_current_in_read_transaction(self.h.conn,self.login.epoch)
        finally:self.h.conn.rollback();self.h.conn.execute('PRAGMA query_only=OFF')

    async def test_gateway_uses_provider_and_never_changes_canonical_data(self):
        internal=FastAPI();check=original_auth()
        def internal_auth(incoming):check(incoming);return CTX
        install_internal(internal,self.journal,authenticate=internal_auth,enabled=True)
        app=FastAPI();sent=[]
        def factory(**options):
            async def record(incoming):sent.append(incoming)
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=internal),event_hooks={'request':[record]},**options)
        install(app,enabled=True,authenticate=self.provider.authenticate,expected_access=CTX,
            expected_origin=ORIGIN,base_url='http://127.0.0.1:3020',internal_token=TOKEN,client_factory=factory)
        before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://relay.synthetic.invalid') as client:
            result=await client.post(PATH.replace('{conversation_id}',CID),json=self.body,headers={
                'authorization':BROWSER,'origin':ORIGIN,'content-type':'application/json'})
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['state'],'attempt_missing')
        self.assertEqual(len(sent),1);self.assertGreaterEqual(self.login_calls,6)
        self.assertEqual(before,list(self.h.conn.iterdump()));self.assertEqual(config,self.h.config_path.read_bytes())
        self.assertNotIn(self.login.epoch.value,result.text);self.assertNotIn(TOKEN,result.text)

    def binding_client(self, *, access=CTX):
        app = FastAPI()
        recovery_binding.install(app, enabled=True, provider=self.provider,
                                 expected_access=access, expected_origin=ORIGIN)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='https://relay.synthetic.invalid',
            headers={'Origin': ORIGIN, 'Authorization': BROWSER},
        )

    async def test_recovery_binding_route_is_opt_in_and_rejects_conflicts(self):
        app = FastAPI()
        self.assertFalse(recovery_binding.install(app, provider=object()))
        self.assertFalse(any(r.path == recovery_binding.PATH for r in app.routes))
        app.get(recovery_binding.PATH.replace('{conversation_id}', '{other}'))(lambda: {})
        with self.assertRaisesRegex(ValueError, 'route_conflict'):
            recovery_binding.install(app, enabled=True, provider=self.provider,
                                     expected_access=CTX, expected_origin=ORIGIN)

    async def test_recovery_binding_exposes_only_restore_fields_and_relogin_changes_version(self):
        path = recovery_binding.PATH.replace('{conversation_id}', CID)
        before = list(self.h.conn.iterdump())
        config = self.h.config_path.read_bytes()
        async with self.binding_client() as client:
            response = await client.post(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
            self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
            first = response.json()
            self.assertEqual(set(first), {'identity', 'conversationId', 'contextVersion'})
            self.assertEqual(first['conversationId'], CID)
            self.assertRegex(first['identity'], r'^recovery:[0-9a-f]{64}$')
            self.assertRegex(first['contextVersion'], r'^recovery-context:[0-9a-f]{64}$')
            self.assertEqual((await client.post(path)).json(), first)
            self.assertEqual(list(self.h.conn.iterdump()), before)
            self.assertEqual(self.h.config_path.read_bytes(), config)
            self.assertNotIn(self.login.epoch.value, response.text)
            self.assertNotIn(self.login_identity.principal_id, response.text)
            self.login = replace(self.login, epoch=self.registry.issue_after_authenticated_login())
            second = (await client.post(path)).json()
            self.assertEqual(first['identity'], second['identity'])
            self.assertNotEqual(first['contextVersion'], second['contextVersion'])
            self.registry.revoke_before_logout()
            revoked = await client.post(path)
            self.assertEqual(revoked.status_code, 403)
            self.assertEqual(revoked.json(), {'ok': False, 'error': 'access_denied'})

    async def test_recovery_binding_rejects_nonempty_requests_and_untrusted_origin(self):
        path = recovery_binding.PATH.replace('{conversation_id}', CID)
        async with self.binding_client() as client:
            for response, code in (
                (await client.post(path, headers={'Origin': 'https://other.synthetic.invalid'}), 403),
                (await client.post(path, json={'identity': 'supplied'}), 400),
                (await client.post(path + '?identity=supplied'), 400),
                (await client.post(path, headers={'X-API-Loop-Internal-Token': TOKEN}), 400),
            ):
                self.assertEqual(response.status_code, code, response.text)
                self.assertEqual(set(response.json()), {'ok', 'error'})
            self.assertEqual(self.login_calls, 0)
            response = await client.post(path, headers={'Authorization': 'invalid'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {'ok': False, 'error': 'auth_required'})

    async def test_recovery_binding_rejects_scope_different_from_status_gateway(self):
        path = recovery_binding.PATH.replace('{conversation_id}', CID)
        other_access = replace(CTX, allowed_scopes=frozenset({'unrelated'}))
        async with self.binding_client(access=other_access) as client:
            response = await client.post(path)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {'ok': False, 'error': 'access_denied'})

    async def test_gateway_refuses_epoch_rotation_after_upstream_observation(self):
        internal=FastAPI();check=original_auth()
        def internal_auth(incoming):check(incoming);return CTX
        install_internal(internal,self.journal,authenticate=internal_auth,enabled=True)
        class RotatingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(_, incoming):
                result=await httpx.ASGITransport(app=internal).handle_async_request(incoming)
                self.login=replace(self.login,epoch=self.registry.issue_after_authenticated_login())
                return result
        def factory(**options):return httpx.AsyncClient(transport=RotatingTransport(),**options)
        app=FastAPI();install(app,enabled=True,authenticate=self.provider.authenticate,expected_access=CTX,
            expected_origin=ORIGIN,base_url='http://127.0.0.1:3020',internal_token=TOKEN,client_factory=factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://relay.synthetic.invalid') as client:
            result=await client.post(PATH.replace('{conversation_id}',CID),json=self.body,headers={
                'authorization':BROWSER,'origin':ORIGIN,'content-type':'application/json'})
        self.assertEqual(result.status_code,403,result.text);self.assertNotIn('state',result.json())
