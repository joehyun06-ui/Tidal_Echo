"""Offline real migration entry + canonical journal + separate private JSON."""
from copy import deepcopy
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
from threading import RLock
import unittest
from unittest.mock import patch

from backend import deployment_config, provider_model_migration as migration
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_model_commands import CoordinatedModelCommands, MIGRATE, ROLLBACK
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as settings_commands

ACCESS = ConfigCommandAccess('synthetic_owner')
OLD, NEW = 'synthetic_old_model', 'synthetic_new_model'
ROUTE = {'model':OLD, 'url':'https://synthetic.invalid/v1', 'key':'synthetic secret'}


def model_commands(c, *, authorize=lambda *_: True, enabled=True):
    return CoordinatedModelCommands(c, session_lock=RLock(), authorize=authorize, enabled=enabled)


def rollback_payload():
    return {'migration_request_id':'migrate', 'migration_base_generation':0,
            'previous_model':OLD, 'migrated_model':NEW}


def worker(path, mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        c = coordinator(path)
        writer = model_commands(c)
        if mode == 'settings':
            settings_commands(c).update_config('settings',{'history_n':77},access=ACCESS)
            return
        if mode == 'hold':
            calls=[]
            def authorize(*_):
                calls.append(1)
                if len(calls)==3:
                    print('ready',flush=True);sys.stdin.readline()
                return True
            writer._authorize=authorize
        operation=lambda: migration.migrate_primary_provider_model(
            c.config, {'model':NEW}, coordinated=writer, request_id='migrate', access=ACCESS,
            expected_generation=0, expected_model=OLD)
        if mode == 'race':
            try:
                writer.migrate_primary_provider_model(c.config,{'model':'second model'},request_id='second',
                    access=ACCESS,expected_generation=0,expected_model=OLD)
            except LifecycleError as error:
                print(error.category,flush=True)
            return
        if mode.startswith('undo_'):
            operation=lambda:writer.rollback_provider_model(c.config,rollback_payload(),request_id='undo',access=ACCESS)
        if mode.startswith('crash_') or mode.startswith('undo_'):
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode.endswith('after'):original(*args)
                os._exit(73)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:operation()


@unittest.skipUnless(os.name=='posix','cooperative private-file candidate requires POSIX')
class ModelCommandTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        (self.root/'gate').mkdir(mode=0o700)
        config=self.h.get('legacy');config['main_chain']=[dict(ROUTE)];config['history_n']=24
        self.h.put('legacy',config);self.c=coordinator(self.h.path);self.c.bootstrap()
        self.writer=model_commands(self.c)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def rejects(self, function, category):
        with self.assertRaises(LifecycleError) as caught:function()
        self.assertEqual(caught.exception.category,category)
        self.assertEqual(str(caught.exception),category)

    def migrate(self, *, writer=None, rid='migrate', model=NEW, base=0, previous=OLD, access=ACCESS, recover=False):
        return migration.migrate_primary_provider_model(self.h.config_path,{'model':model},
            coordinated=writer or self.writer,request_id=rid,access=access,
            expected_generation=base,expected_model=previous,recover=recover)

    def undo(self, *, writer=None, payload=None, rid='undo', access=ACCESS, recover=False):
        return (writer or self.writer).rollback_provider_model(self.h.config_path,
            rollback_payload() if payload is None else payload, request_id=rid,access=access,recover=recover)

    def generation(self):return self.h.conn.execute('SELECT generation FROM session_coordination_meta').fetchone()[0]

    def test_default_off_and_denied_access_do_not_touch_dependencies(self):
        def forbidden(*_):self.fail('dependency used')
        writer=CoordinatedModelCommands(None,session_lock=None,authorize=forbidden)
        self.rejects(lambda:self.migrate(writer=writer),'lifecycle_disabled')
        self.rejects(lambda:self.undo(writer=writer),'lifecycle_disabled')
        denied=model_commands(None,authorize=lambda *_:False)
        self.rejects(lambda:self.migrate(writer=denied),'coordination_access_denied')

    def test_real_entry_changes_only_model_and_returns_private_receipt(self):
        before=self.h.get('legacy')
        with patch.object(migration,'_restore_original',side_effect=AssertionError('must not restore')):
            receipt=self.migrate()
        expected=deepcopy(before);expected['main_chain'][0]['model']=NEW
        self.assertEqual(self.h.get('legacy'),expected)
        self.assertEqual((receipt.generation,receipt.replayed),(1,False))
        self.assertNotIn(ROUTE['key'],repr(receipt));self.assertNotIn(ROUTE['url'],repr(receipt))

    def test_same_model_still_records_an_idempotent_config_command(self):
        self.assertEqual(self.migrate(model=OLD).generation,1)
        self.assertTrue(self.migrate(model=OLD).replayed)
        self.assertEqual(self.generation(),1)

    def test_pure_candidate_preserves_input_and_requires_materialized_route(self):
        config=self.h.get('legacy');before=deepcopy(config)
        result,previous=migration.prepare_model_only_change(config,{'model':NEW})
        self.assertEqual(config,before);self.assertEqual(previous,OLD)
        result['sessions'][0]['title']='changed';self.assertEqual(config,before)
        with self.assertRaises(migration.ProviderModelMigrationError) as caught:
            migration.prepare_model_only_change({}, {'model':NEW})
        self.assertEqual(caught.exception.category,migration.CONFIG_UNAVAILABLE)

    def test_expected_generation_or_model_conflict_never_creates_intent(self):
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.migrate(base=1),'coordination_cas_conflict')
        self.rejects(lambda:self.migrate(previous='wrong model'),'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_invalid_generation_and_model_payload_are_rejected(self):
        for generation in (True,-1,1.5,2**63):
            with self.subTest(generation=generation):self.rejects(lambda:self.migrate(base=generation),'coordination_invalid')
        with self.assertRaises(migration.ProviderModelMigrationError):self.migrate(model='invalid\nmodel')
        with self.assertRaises(migration.ProviderModelMigrationError):
            migration.migrate_primary_provider_model(self.h.config_path,{'model':NEW,'key':'synthetic secret'},
                coordinated=self.writer,request_id='bad',access=ACCESS,expected_generation=0,expected_model=OLD)

    def test_command_is_bound_to_exact_private_config_path(self):
        other=self.root/'other.json'
        self.rejects(lambda:migration.migrate_primary_provider_model(other,{'model':NEW},coordinated=self.writer,
            request_id='migrate',access=ACCESS,expected_generation=0,expected_model=OLD),'coordination_resource_mismatch')
        self.assertFalse(other.exists())

    def test_candidate_never_reads_environment_route_or_legacy_restore(self):
        with (patch.object(migration,'_load_environment_primary_route',side_effect=AssertionError('env read')),
              patch.object(migration,'_restore_original',side_effect=AssertionError('legacy undo'))):
            self.migrate()

    def test_metadata_without_candidate_cannot_fall_back_to_legacy_write(self):
        before=self.h.config_path.read_bytes()
        for fields in ({'request_id':'id'},{'recover':True},{'expected_generation':0},{'access':ACCESS}):
            with self.subTest(fields=fields),self.assertRaises(migration.ProviderModelMigrationError):
                migration.migrate_primary_provider_model(self.h.config_path,{'model':NEW},**fields)
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_migration_replay_is_historical_and_does_not_resend_after_undo(self):
        self.migrate();self.undo()
        self.assertTrue(self.migrate().replayed)
        self.assertEqual(self.h.get('legacy')['main_chain'][0]['model'],OLD)
        self.assertEqual(self.generation(),2)

    def test_migration_identity_binds_model_precondition_and_principal(self):
        self.migrate()
        self.rejects(lambda:self.migrate(model='other model'),'coordination_request_conflict')
        self.rejects(lambda:self.migrate(base=1),'coordination_request_conflict')
        self.rejects(lambda:self.migrate(previous='other model'),'coordination_request_conflict')
        self.rejects(lambda:self.migrate(access=ConfigCommandAccess('other_owner')),'coordination_request_conflict')

    def test_genuine_undo_preserves_url_key_sessions_and_other_settings(self):
        before=self.h.get('legacy');self.migrate()
        receipt=self.undo();self.assertEqual(receipt.generation,2)
        self.assertEqual(self.h.get('legacy'),before)
        self.assertTrue(self.undo().replayed);self.assertEqual(self.generation(),2)

    def test_undo_after_unrelated_cooperative_update_refuses_without_overwrite(self):
        self.migrate();settings_commands(self.c).update_config('settings',{'history_n':77},access=ACCESS)
        before=self.h.config_path.read_bytes()
        self.rejects(self.undo,'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.get('legacy')['history_n'],77)

    def test_undo_after_later_model_change_cannot_restore_older_model(self):
        self.migrate();self.migrate(rid='later',model='later model',base=1,previous=NEW)
        before=self.h.config_path.read_bytes()
        self.rejects(self.undo,'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_undo_rejects_unrecorded_or_other_action_source(self):
        settings_commands(self.c).update_config('migrate',{'history_n':3},access=ACCESS)
        before=self.h.config_path.read_bytes()
        self.rejects(self.undo,'coordination_operation_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_undo_cannot_forge_previous_model_or_original_actor(self):
        self.migrate();payload=rollback_payload();payload['previous_model']='forged old model'
        self.rejects(lambda:self.undo(payload=payload),'coordination_operation_conflict')
        self.rejects(lambda:self.undo(access=ConfigCommandAccess('other_owner')),'coordination_operation_conflict')
        payload=rollback_payload();payload['migration_request_id']='missing'
        self.rejects(lambda:self.undo(payload=payload),'coordination_operation_conflict')

    def test_undo_requires_separate_current_authorization(self):
        self.migrate();writer=model_commands(self.c,authorize=lambda _a,action,_t:action==MIGRATE)
        self.rejects(lambda:self.undo(writer=writer),'coordination_access_denied')

    def test_undo_replay_identity_and_authorization_are_checked(self):
        self.migrate();self.undo();payload=rollback_payload();payload['migrated_model']='changed'
        self.rejects(lambda:self.undo(payload=payload),'coordination_request_conflict')
        denied=model_commands(self.c,authorize=lambda *_:False)
        self.rejects(lambda:self.undo(writer=denied),'coordination_access_denied')

    def test_removed_or_corrupt_source_receipt_blocks_undo(self):
        self.migrate();before=self.h.config_path.read_bytes()
        self.h.conn.execute("UPDATE session_coordination_ops SET input_sha=? WHERE request_id='migrate'",('a'*64,))
        self.rejects(self.undo,'coordination_operation_conflict')
        self.h.conn.execute("DELETE FROM session_coordination_ops WHERE request_id='migrate'")
        self.rejects(self.undo,'coordination_operation_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_uncooperative_json_change_is_not_overwritten_by_undo(self):
        self.migrate();config=self.h.get('legacy');config['external']=True;self.h.put('legacy',config)
        before=self.h.config_path.read_bytes()
        self.rejects(self.undo,'coordination_external_change')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_pending_undo_recovery_revalidates_source_receipt(self):
        self.migrate()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('publish failed')):
            self.rejects(self.undo,'coordination_unavailable')
        before=self.h.config_path.read_bytes()
        self.h.conn.execute("DELETE FROM session_coordination_ops WHERE request_id='migrate'")
        self.rejects(lambda:self.undo(recover=True),'coordination_operation_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_current_access_is_rechecked_for_pending_migration_recovery(self):
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('publish failed')):
            self.rejects(self.migrate,'coordination_unavailable')
        denied=model_commands(self.c,authorize=lambda *_:False)
        self.rejects(lambda:self.migrate(writer=denied,recover=True),'coordination_access_denied')
        self.rejects(lambda:self.migrate(model='another model',recover=True),'coordination_request_conflict')
        with patch('backend.session_lifecycle_model_commands.prepare_model_only_change',side_effect=AssertionError('callback replay')):
            self.assertEqual(self.migrate(recover=True).generation,1)

    def test_sql_journal_and_receipts_do_not_contain_routes_or_model_text(self):
        receipt=self.migrate();rows=self.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall()
        for value in (ROUTE['key'],ROUTE['url'],OLD,NEW):
            self.assertNotIn(value,repr(rows));self.assertNotIn(value,repr(receipt))
        self.assertFalse(self.writer.production_supported);self.assertFalse(self.writer.http_integrated)
        self.assertFalse(self.writer.all_writers_integrated)

    def child(self,mode):
        process=subprocess.Popen([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_model_commands import worker; worker(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup);return process

    def test_model_and_settings_processes_preserve_both_changes_then_refuse_stale_undo(self):
        first=self.child('hold')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        second=self.child('settings')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(timeout=.15)
        _,err1=first.communicate('\n',timeout=10);_,err2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),err1+err2)
        config=self.h.get('legacy');self.assertEqual(config['history_n'],77)
        self.assertEqual(config['main_chain'][0]['model'],NEW)
        self.rejects(self.undo,'coordination_cas_conflict')

    def test_concurrent_model_commands_with_same_base_have_one_winner(self):
        first=self.child('hold')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        second=self.child('race')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(timeout=.15)
        _,err1=first.communicate('\n',timeout=10);out2,err2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),err1+err2)
        self.assertEqual(out2.strip(),'coordination_cas_conflict')
        self.assertEqual(self.generation(),1)
        self.assertEqual(self.h.get('legacy')['main_chain'][0]['model'],NEW)

    def test_process_crash_before_publish_recovers_original_migration(self):
        process=self.child('crash_before');_,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,73,error)
        self.assertEqual(self.h.get('legacy')['main_chain'][0]['model'],OLD)
        self.assertEqual(self.migrate(recover=True).generation,1)

    def test_process_crash_after_publish_does_not_republish(self):
        process=self.child('crash_after');_,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,73,error)
        self.assertEqual(self.h.get('legacy')['main_chain'][0]['model'],NEW)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish')):
            self.assertEqual(self.migrate(recover=True).generation,1)

    def test_process_crash_in_undo_recovers_without_full_file_restore(self):
        before=self.h.get('legacy');self.migrate()
        process=self.child('undo_after');_,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,73,error)
        with (patch.object(migration,'_restore_original',side_effect=AssertionError('legacy undo')),
              patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish'))):
            self.assertEqual(self.undo(recover=True).generation,2)
        self.assertEqual(self.h.get('legacy'),before)


if __name__=='__main__':unittest.main()
