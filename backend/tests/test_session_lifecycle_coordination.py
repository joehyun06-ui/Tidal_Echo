"""Offline process/crash tests using actual JSON writer/session authority/DDL."""
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import channel_store, deployment_config
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_coordination import SharedSessionCoordinator
from backend.session_lifecycle_storage import SQLiteLifecycleRepository
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_session_lifecycle import CID, CTX, ACTIVE
from backend.tests.test_session_lifecycle_storage import FileHarness


def validator(config):
    deployment_config.validate_loop_config_payload(config, render_mvp=True)
    class Legacy:
        def load_config(self): return config
    WebSessionProviderAuthority(Legacy()).session_rows()


def coordinator(path, *, enabled=True, timeout=2):
    path = Path(path)
    return SharedSessionCoordinator(lambda: channel_store.connect(path), path.with_suffix('.json'),
        path.parent/'gate', validate=validator, enabled=enabled, timeout=timeout)


def worker(path, mode):
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')):
        c = coordinator(path)
        if mode == 'hold':
            with c.fence():
                print('locked', flush=True)
                sys.stdin.readline()
            return
        body = {'title': 'process title'} if mode == 'slow' else {'pinned': True}
        def modify(legacy):
            if mode == 'slow':
                print('loaded', flush=True)
                sys.stdin.readline()
            WebSessionProviderAuthority(legacy).patch_session(CID, body)
        if mode.startswith('crash_'):
            original = deployment_config.atomic_write_text
            def publish(*args):
                if mode == 'crash_after': original(*args)
                os._exit(71)
            with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=publish):
                c.mutate('crash', body, modify)
        else:
            receipt = c.mutate(mode, body, modify)
            print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}), flush=True)


@unittest.skipUnless(os.name == 'posix', 'POSIX local-lock candidate')
class CoordinationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.h = FileHarness(self.root/'relay.sqlite')
        self.addCleanup(self.h.conn.close)
        (self.root/'gate').mkdir(mode=0o700)
        self.c = coordinator(self.h.path)
        self.assertTrue(self.c.bootstrap())
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline')))

    def rejects(self, function, category):
        with self.assertRaises(LifecycleError) as caught: function()
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), category)

    def child(self, mode):
        process = subprocess.Popen([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_coordination import worker; worker(sys.argv[1],sys.argv[2])',
            self.h.path, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None: process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def line(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), 'child did not reach checkpoint')
            return child.stdout.readline().strip()

    def modify(self, body, rid='patch'):
        return self.c.mutate(rid, body, lambda legacy: WebSessionProviderAuthority(legacy).patch_session(CID, body))

    def meta(self):
        return tuple(self.h.conn.execute('SELECT generation,config_sha FROM session_coordination_meta').fetchone())

    def test_default_off_does_no_io(self):
        def forbidden(): raise AssertionError('IO forbidden')
        c = SharedSessionCoordinator(forbidden, '/missing.json', '/missing', validate=forbidden)
        self.rejects(c.bootstrap, 'lifecycle_disabled')
        self.rejects(lambda: c.mutate('id', {}, forbidden), 'lifecycle_disabled')

    def test_actual_authority_patch_and_same_id_replay(self):
        self.assertFalse(self.c.bootstrap())
        result = self.modify({'title':'new title'})
        before = self.h.config_path.read_bytes()
        self.assertEqual((result.generation, result.replayed), (1, False))
        replay = self.c.mutate('patch', {'title':'new title'}, lambda _: self.fail('replayed callback'))
        self.assertEqual((replay.generation, replay.replayed), (1, True))
        self.assertEqual(self.h.config_path.read_bytes(), before)
        self.rejects(lambda: self.modify({'title':'different'}), 'coordination_request_conflict')
        self.assertEqual(self.h.get('legacy')['sessions'][0]['title'], 'new title')

    def test_two_process_rmw_preserves_both_changes(self):
        first = self.child('slow'); self.assertEqual(self.line(first), 'loaded')
        second = self.child('fast')
        first_out, first_err = first.communicate('\n', timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        self.assertEqual((first.returncode, second.returncode), (0, 0), first_err + second_err)
        self.assertEqual(json.loads(first_out)['generation'], 1)
        self.assertEqual(json.loads(second_out)['generation'], 2)
        row = self.h.get('legacy')['sessions'][0]
        self.assertEqual(row['title'], 'process title'); self.assertTrue(row['pinned'])

    def test_cross_process_timeout_and_kernel_release_after_death(self):
        child = self.child('hold'); self.assertEqual(self.line(child), 'locked')
        c = coordinator(self.h.path, timeout=.05)
        self.rejects(lambda: c.mutate('busy', {}, lambda _: None), 'coordination_busy')
        child.kill(); child.communicate(timeout=5)
        self.assertEqual(self.modify({'pinned':True}).generation, 1)

    def crash(self, mode):
        child = self.child(mode); _, errors = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 71, errors)
        self.assertEqual(self.h.conn.execute("SELECT status FROM session_coordination_ops WHERE request_id='crash'").fetchone()[0], 'pending')

    def enter_fence(self):
        with self.c.fence(): pass

    def test_crash_after_intent_before_publish_is_explicitly_recovered(self):
        old = self.h.config_path.read_bytes(); self.crash('crash_before')
        self.assertEqual(self.h.config_path.read_bytes(), old)
        self.rejects(self.enter_fence, 'coordination_recovery_required')
        self.rejects(lambda: self.modify({'title':'blocked'}), 'coordination_recovery_required')
        receipt = self.c.recover('crash')
        self.assertEqual((receipt.generation,receipt.replayed), (1,True))
        self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])
        self.assertEqual(self.c.recover('crash').generation, 1)
        self.assertFalse(list((self.root/'gate').glob('*.stage')))

    def test_crash_after_publish_finalizes_without_republishing(self):
        self.crash('crash_after')
        self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])
        with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=AssertionError('must not republish')):
            self.assertEqual(self.c.recover('crash').generation, 1)
        self.assertEqual(self.meta()[0], 1)

    def test_missing_stage_refuses_without_overwriting_json(self):
        self.crash('crash_before')
        stage = next((self.root/'gate').glob('*.stage')); stage.unlink()
        old = self.h.config_path.read_bytes()
        self.rejects(lambda: self.c.recover('crash'), 'coordination_stage_unavailable')
        self.assertEqual(self.h.config_path.read_bytes(), old)

    def test_corrupt_stage_refuses_without_overwriting_json(self):
        self.crash('crash_before')
        next((self.root/'gate').glob('*.stage')).write_text('{}')
        old = self.h.config_path.read_bytes()
        self.rejects(lambda: self.c.recover('crash'), 'coordination_stage_invalid')
        self.assertEqual(self.h.config_path.read_bytes(), old)

    def test_external_edit_pending_or_ready_requires_reconciliation(self):
        self.crash('crash_before')
        config = self.h.get('legacy'); config['external'] = True; self.h.put('legacy', config)
        self.rejects(lambda: self.c.recover('crash'), 'coordination_external_change')
        self.assertTrue(self.h.get('legacy')['external'])

    def test_external_writer_bypassing_gate_is_not_claimed_protected(self):
        config = self.h.get('legacy'); config['external'] = True; self.h.put('legacy', config)
        self.rejects(self.enter_fence, 'coordination_external_change')
        self.assertFalse(self.c.production_supported); self.assertFalse(self.c.all_writers_integrated)

    def test_cooperative_writer_cannot_cross_repository_final_check(self):
        repo = SQLiteLifecycleRepository(self.h.connect_canonical, self.h.reader, coordinator=self.c, enabled=True)
        self.h.engine.repository = repo
        with repo.transaction(CID):
            child = self.child('fast')
            with self.assertRaises(subprocess.TimeoutExpired): child.communicate(timeout=.15)
        out, error = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(json.loads(out)['generation'], 1)
        self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0, active_state=ACTIVE, access=CTX)
        self.assertEqual(self.h.record().phase, 'closed')
        self.assertFalse(repo.cross_store_atomic)

    def test_nested_fence_is_supported_but_nested_write_refused(self):
        with self.c.fence():
            with coordinator(self.h.path).fence(): pass
            self.rejects(lambda: self.modify({'pinned':True}), 'coordination_nested_write')

    def test_nested_read_fence_does_not_take_second_sqlite_write_lock(self):
        repo = SQLiteLifecycleRepository(self.h.connect_canonical, self.h.reader, coordinator=self.c, enabled=True)
        with repo.transaction(CID):
            with self.c.fence(): pass

    def test_validation_failure_never_creates_intent(self):
        def invalid(legacy):
            config = legacy.load_config(); config['main_chain'] = ['invalid']; legacy.save_config(config)
        with self.assertRaises(deployment_config.DeploymentConfigError): self.c.mutate('invalid', {}, invalid)
        self.assertEqual(self.meta()[0], 0)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0], 0)

    def test_world_readable_gate_or_config_is_refused(self):
        (self.root/'gate').chmod(0o755)
        self.rejects(self.enter_fence, 'coordination_private_directory_required')
        (self.root/'gate').chmod(0o700); self.h.config_path.chmod(0o644)
        self.rejects(self.enter_fence, 'coordination_private_file_required')

    def test_symlink_lock_is_refused_and_target_is_untouched(self):
        lock = self.root/'gate'/'writer.lock'; lock.unlink()
        target = self.root/'secret'; target.write_text('synthetic secret')
        lock.symlink_to(target)
        self.rejects(self.enter_fence, 'coordination_unavailable')
        self.assertEqual(target.read_text(), 'synthetic secret')

    def test_gate_cannot_be_rebound_to_another_config_or_database(self):
        other = self.root/'other.json'; deployment_config.atomic_write_text(other, self.h.config_path.read_text())
        c = SharedSessionCoordinator(self.h.connect_canonical, other, self.root/'gate', validate=validator, enabled=True)
        self.rejects(c.bootstrap, 'coordination_schema_invalid')

    def test_restart_replay_does_not_repeat_mutator(self):
        self.modify({'pinned':True})
        c = coordinator(self.h.path)
        self.assertTrue(c.mutate('patch', {'pinned':True}, lambda _: self.fail('callback replayed')).replayed)

    def test_no_config_secrets_in_sql_journal(self):
        config = self.h.get('legacy'); config['main_chain'] = [{'url':'https://synthetic.invalid','key':'synthetic secret','model':'synthetic'}]
        self.c.mutate('secret', {'action':'set-route'}, lambda legacy: legacy.save_config(config))
        rows = self.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall()
        self.assertNotIn('synthetic secret', repr(rows))
        self.assertNotIn('synthetic.invalid', repr(rows))

    def test_unexpected_trigger_is_refused_before_writes(self):
        self.h.conn.execute('CREATE TRIGGER unexpected AFTER UPDATE ON session_coordination_meta BEGIN SELECT 1; END')
        self.rejects(lambda: self.modify({'pinned':True}), 'coordination_schema_invalid')

    def test_publish_failure_leaves_recoverable_durable_intent(self):
        with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=OSError('synthetic failure')):
            self.rejects(lambda: self.modify({'pinned':True}), 'coordination_unavailable')
        self.assertEqual(self.c.recover('patch').generation, 1)
        self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])

    def test_finalization_sql_failure_keeps_published_target_and_pending_intent(self):
        original = deployment_config.atomic_write_text
        def publish(*args):
            original(*args)
            # A separate canonical writer has the write lock at finalize time.
            blocker = channel_store.connect(self.h.path)
            blocker.execute('BEGIN IMMEDIATE')
            self.addCleanup(blocker.close)
            self.blocker = blocker
        def fast_connect():
            conn = channel_store.connect(self.h.path); conn.execute('PRAGMA busy_timeout=1'); return conn
        self.c._connect = fast_connect
        with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=publish):
            self.rejects(lambda: self.modify({'pinned':True}), 'coordination_unavailable')
        self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])
        self.assertEqual(self.meta()[0], 0)
        self.blocker.rollback()
        self.assertEqual(self.c.recover('patch').generation, 1)

    def test_committed_cleanup_failure_reconciles_without_callback_or_publish(self):
        original = self.c._sync_directory
        calls = []
        def sync():
            calls.append(1)
            if len(calls) == 2: raise OSError('cleanup interrupted')
            original()
        with patch.object(self.c, '_sync_directory', side_effect=sync):
            self.rejects(lambda: self.modify({'pinned':True}), 'coordination_unavailable')
        self.assertEqual(self.meta()[0], 1)
        with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=AssertionError('must not publish')):
            self.assertTrue(self.c.recover('patch').replayed)


if __name__ == '__main__': unittest.main()
