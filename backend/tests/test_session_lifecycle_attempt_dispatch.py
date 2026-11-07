"""Synthetic adapter only; real temporary SQLite/JSON, network forbidden."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.session_window_attempt_dispatch import attempt_dispatch_once
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


def crash_worker(path):
    patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')).start()
    patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')).start()
    h = FileHarness(path, seed=False)
    state = h.record()
    def dispatch(observation):
        Path(path+'.adapter-marker').write_text(observation.request_id)
        os._exit(17)
    attempt_dispatch_once(h.engine, WindowAttemptJournal(h.repo, enabled=True), CID, 'attempt_1',
        packet_sha256=state.packet_sha256, expected_revision=state.revision,
        access=CTX, dispatch=dispatch, enabled=True)


@unittest.skipUnless(os.name == 'posix', 'private JSON fixture uses POSIX')
class DispatchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'canonical.sqlite'
        self.h=FileHarness(self.path); self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')))
        with closing(self.h.connect_canonical()) as conn: install_schema(conn)
        self.journal=WindowAttemptJournal(self.h.repo,enabled=True)
        self.h.engine.closeout(CID,'close_1',window_id='window_0',context_epoch=0,access=CTX)
        self.h.engine.reentry(CID,'prepare_1',closeout_id='close_1',target_window_id='window_1',access=CTX)
        self.state=self.h.record(); self.calls=[]

    def dispatch(self, observation): self.calls.append(observation)

    def run_attempt(self, dispatch=None, **kwargs):
        args=dict(packet_sha256=self.state.packet_sha256,expected_revision=self.state.revision,
                  access=CTX,enabled=True)
        args.update(kwargs)
        # A plain trusted function is required, not an arbitrary callable object.
        if dispatch is None:
            def dispatch(observation): self.calls.append(observation)
        return attempt_dispatch_once(self.h.engine,self.journal,CID,'attempt_1',dispatch=dispatch,**args)

    def rejects(self, callback, category):
        with self.assertRaises(LifecycleError) as caught: callback()
        self.assertEqual(caught.exception.category,category)

    def test_default_off_has_no_io_or_callback(self):
        self.rejects(lambda: attempt_dispatch_once(None,None,CID,'id',packet_sha256='',
            expected_revision=0,access=None,dispatch=None),'lifecycle_disabled')

    def test_callback_occurs_once_and_return_value_cannot_confirm_window(self):
        def dispatch(observation):
            self.calls.append(observation)
            return {'status':'confirmed','window_confirmed':True}
        result=self.run_attempt(dispatch)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0].request_id,'attempt_1')
        self.assertEqual(self.calls[0].packet_sha256,self.state.packet_sha256)
        self.assertEqual(result.outcome,'unknown'); self.assertFalse(result.window_confirmed)
        self.assertFalse(result.dispatch_authorized)
        self.assertTrue(self.run_attempt(dispatch).replayed)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.h.record(),self.state)

    def test_callback_runs_after_commit_outside_sql_transaction_inside_authority_fence(self):
        parent=self.h.fence; held=[]
        @contextmanager
        def fence(*args,**kwargs):
            with parent(*args,**kwargs) as snapshot:
                held.append(True)
                try: yield snapshot
                finally: held.pop()
        def dispatch(observation):
            self.assertEqual(held,[True])
            with closing(self.h.connect_canonical()) as conn:
                conn.execute('BEGIN IMMEDIATE')
                self.assertEqual(conn.execute('SELECT count(*) FROM session_window_attempts').fetchone()[0],1)
                conn.rollback()
            self.calls.append(observation)
        with patch.object(self.h,'fence',side_effect=fence): self.run_attempt(dispatch)
        self.assertEqual(len(self.calls),1)

    def test_callback_exception_keeps_unknown_row_and_does_not_retry(self):
        def dispatch(observation):
            self.calls.append(observation)
            raise OSError('synthetic private adapter error')
        self.rejects(lambda:self.run_attempt(dispatch),'lifecycle_dispatch_unknown')
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.run_attempt(dispatch).replayed)
        self.assertEqual(len(self.calls),1)

    def test_lost_commit_ack_never_invokes_adapter_or_retries(self):
        def lost(): raise OSError('synthetic lost commit acknowledgement')
        self.h.connection_wrapper=lambda conn:Proxy(conn,after=lost)
        self.rejects(self.run_attempt,'lifecycle_storage_unavailable')
        self.h.connection_wrapper=None
        self.assertTrue(self.run_attempt().replayed)
        self.assertEqual(self.calls,[])

    def test_postcommit_validation_failure_has_no_callback_and_keeps_record(self):
        calls=[]
        def validate(snapshot,*,access):
            calls.append(1);return len(calls)<3
        with patch.object(self.h,'validate',side_effect=validate):
            self.rejects(self.run_attempt,'lifecycle_authority_changed_after_commit')
        self.assertTrue(self.run_attempt().replayed)
        self.assertEqual(self.calls,[])

    def test_stale_packet_or_busy_runtime_has_no_callback(self):
        self.h.source('event_2','synthetic new evidence')
        self.rejects(self.run_attempt,'lifecycle_delta_required')
        self.assertEqual(self.calls,[])

    def test_async_generator_and_callable_objects_rejected_before_recording(self):
        async def asynchronous(observation): pass
        def generator(observation): yield 1
        async def asyncgenerator(observation): yield 1
        for adapter in (asynchronous,generator,asyncgenerator,self.dispatch,None):
            self.rejects(lambda:attempt_dispatch_once(self.h.engine,self.journal,CID,'attempt_1',
                packet_sha256=self.state.packet_sha256,expected_revision=self.state.revision,
                access=CTX,dispatch=adapter,enabled=True),'lifecycle_dispatch_adapter_invalid')
        self.assertIsNone(self.journal.inspect(CID,'attempt_1',access=CTX))

    def test_returned_coroutine_is_not_run_and_record_remains_unknown(self):
        async def later(): self.calls.append('unexpected')
        def dispatch(observation): return later()
        self.rejects(lambda:self.run_attempt(dispatch),'lifecycle_dispatch_unknown')
        self.assertTrue(self.run_attempt().replayed)
        self.assertEqual(self.calls,[])

    def test_crash_inside_adapter_reopens_original_attempt_without_second_call(self):
        result=subprocess.run([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_attempt_dispatch import crash_worker; crash_worker(sys.argv[1])',
            str(self.path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,17,result.stderr)
        self.assertEqual(Path(str(self.path)+'.adapter-marker').read_text(),'attempt_1')
        self.assertTrue(self.run_attempt().replayed)
        self.assertEqual(self.calls,[])
