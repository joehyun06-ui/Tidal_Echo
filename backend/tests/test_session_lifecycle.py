"""Synthetic canonical SQLite + real session authority class; no live adapters.

The SQLite-backed legacy config is a TEST surrogate. Production config is a
separate file; these tests do not prove that real cross-store commit fence.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import inspect
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from backend.g3_projection_contract import AccessScope, G3SourceRef
from backend.session_lifecycle import (
    LifecycleDeltaPage, LifecycleError, LifecycleReceipt, LifecycleRecord,
    SessionLifecycle, SessionSnapshot, SourceCursor, WindowReceipt,
)
from backend.tests.test_g3_projection_feed import SyntheticPagedG3
from backend.web_session_provider_authority import WebSessionProviderAuthority


CID = 'synthetic_conversation'
CTX = AccessScope('synthetic_owner', frozenset({'private_A'}))
ACTIVE = (('next_task', 'synthetic pending task'),)


def encode(value):
    return json.dumps(asdict(value), sort_keys=True, separators=(',', ':'))


def decode_record(raw):
    value = json.loads(raw)
    value['source_cursor'] = SourceCursor(**value['source_cursor'])
    for key in ('pending', 'used_windows', 'used_model_runs', 'message_refs',
                'closeout_checks', 'closeout_evidence_candidates', 'summary_candidates'):
        value[key] = tuple(value[key])
    value['active_state'] = tuple(tuple(x) for x in value['active_state'])
    for key in ('anchor_refs', 'recent_refs'):
        value[key] = tuple(G3SourceRef(**ref) for ref in value[key])
    return LifecycleRecord(**value)


class Harness:
    """All physical state is disposable and shares ONE canonical fixture file."""
    def __init__(self, path, *, seed=True, provider='api'):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.lock = RLock()
        self.fail_commit = None
        self.save_conflict = False
        self.page_hook = None
        self.validation_hook = None
        self.snapshot_hook = None
        self.page_size = 1
        if seed:
            self.conn.executescript('''
                CREATE TABLE fixture_config(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,kind TEXT,direction TEXT,text TEXT,meta TEXT);
                CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT,parent_id TEXT,
                    mode TEXT,message_id INTEGER,point_key INTEGER,prompt_key INTEGER);
                CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT);
                CREATE TABLE fixture_g3(cursor INTEGER PRIMARY KEY,eid TEXT,body BLOB,options TEXT);
                CREATE TABLE fixture_window_receipts(digest TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE lifecycle_candidate_state(conversation_id TEXT PRIMARY KEY,revision INTEGER,payload TEXT);
                CREATE TABLE lifecycle_candidate_receipts(conversation_id TEXT,request_id TEXT,payload TEXT,
                    PRIMARY KEY(conversation_id,request_id));
            ''')
            self.put('legacy', {'api_session': CID, 'sessions': [{'id': CID, 'title': 'synthetic chat',
                     'provider': provider, 'since_id': 0, 'created_at': '2026-11-04T00:00:00Z'}]})
            self.put('runtime', {'window_id': 'window_0', 'model_run_id': 'run_0', 'epoch': 0,
                'observed_source': 1, 'observed_message': 1, 'generation': 'synthetic_g3',
                'pending': ['task_1'], 'settled': [], 'anchors': ['anchor_1'], 'recent': ['event_1'],
                'streaming': False, 'tool_inflight': False, 'uncertain_dispatch': False, 'denied': []})
            self.conn.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                              (1, 'user', 'in', 'synthetic original message', json.dumps({'api_session': CID})))
            self.conn.execute('INSERT INTO fixture_g3 VALUES (?,?,?,?)',
                              (1, 'event_1', b'synthetic source text', '{}'))
            # The same cursor cannot contain two events. Anchor is observed too.
            self.conn.execute('INSERT INTO fixture_g3 VALUES (?,?,?,?)',
                              (2, 'anchor_1', b'synthetic reviewed anchor', json.dumps({'layer':'fixed','reviewed':True})))
            runtime = self.get('runtime'); runtime['observed_source'] = 2; self.put('runtime', runtime)
        self.g3 = SyntheticPagedG3()
        for cursor, eid, body, options in self.conn.execute('SELECT * FROM fixture_g3 ORDER BY cursor'):
            self.g3.add_revision(eid, body, cursor=cursor, **json.loads(options))
        self.g3._denied = set(tuple(v) for v in self.get('runtime')['denied'])
        self.session_authority = WebSessionProviderAuthority(self)
        self.engine = SessionLifecycle(self, self, self.g3, self, enabled=True,
                                       supported_providers=frozenset({'api', 'codex'}))

    def get(self, key):
        return json.loads(self.conn.execute('SELECT payload FROM fixture_config WHERE key=?', (key,)).fetchone()[0])

    def put(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO fixture_config VALUES (?,?)', (key, json.dumps(value)))

    def load_config(self):
        return self.get('legacy')

    def source(self, eid, text, **options):
        cursor = max((e.cursor for e in self.g3._timeline), default=0) + 1
        event = self.g3.add_revision(eid, text, cursor=cursor, **options)
        raw = text.encode() if isinstance(text, str) else text
        self.conn.execute('INSERT INTO fixture_g3 VALUES (?,?,?,?)', (cursor, eid, raw, json.dumps(options)))
        return event

    def message(self, text='synthetic new message'):
        selected = self.selected()
        cur = self.conn.execute('INSERT INTO messages(kind,direction,text,meta) VALUES (?,?,?,?)',
                               ('user', 'in', text, json.dumps({'api_session': selected})))
        return cur.lastrowid

    def selected(self):
        row = self.conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?', (CID,)).fetchone()
        return row[0] if row else CID

    def snapshot(self, cid, access):
        if access.principal_id != CTX.principal_id:
            raise LifecycleError('lifecycle_access_denied')
        if cid != CID or self.session_authority.tombstone_for_session(cid):
            raise LifecycleError('lifecycle_session_unavailable')
        row = self.session_authority.row_for_session(cid)
        if row is None:
            raise LifecycleError('lifecycle_session_unavailable')
        version = self.selected()
        selected = self.session_authority.row_for_session(version)
        if selected is None or selected['provider'] != row['provider']:
            raise LifecycleError('lifecycle_session_unavailable')
        if version != cid:
            edge = self.conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (version,)).fetchone()
            if edge is None or edge[0] != cid:
                raise LifecycleError('lifecycle_session_unavailable')
        runtime = self.get('runtime')
        head = max((e.cursor for e in self.g3._timeline), default=0)
        message = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE json_extract(meta,'$.api_session')=?",
                                    (version,)).fetchone()[0]
        def visible(eid):
            event = self.g3._heads[eid]
            return (event.status == 'active' and event.ref.scope in access.allowed_scopes
                    and (access.principal_id, event.ref.scope) not in self.g3._denied)
        anchors = tuple(e for e in runtime['anchors'] if visible(e))
        recent = tuple(e for e in runtime['recent'] if visible(e))
        snapshot = SessionSnapshot(cid, row['provider'], version, runtime['window_id'], runtime['model_run_id'],
             runtime['epoch'], 'pending_stamp', SourceCursor(runtime['generation'], runtime['observed_source']),
             runtime['observed_message'], SourceCursor(runtime['generation'], head), message,
             tuple(runtime['pending']), tuple(runtime['settled']), anchors, recent,
             runtime['streaming'], runtime['tool_inflight'], runtime['uncertain_dispatch'])
        digest = hashlib.sha256(json.dumps([asdict(snapshot), sorted(self.g3._denied),
                      [asdict(e) for e in self.g3._heads.values()]], sort_keys=True).encode()).hexdigest()
        result = replace(snapshot, stamp=digest)
        return self.snapshot_hook(result) if self.snapshot_hook else result

    @contextmanager
    def fence(self, cid, *, access):
        with self.lock:
            yield self.snapshot(cid, access)

    def validate(self, snapshot, *, access):
        if self.validation_hook:
            self.validation_hook()
        return self.snapshot(snapshot.conversation_id, access) == snapshot

    def read_delta(self, snapshot, *, source_after, message_after, access):
        events = tuple(e for e in self.g3._timeline if source_after < e.cursor <= snapshot.source_head.position)[:self.page_size]
        source_next = events[-1].cursor if events else snapshot.source_head.position
        visible = tuple(e for e in events if e.ref.scope in access.allowed_scopes
                        and (access.principal_id, e.ref.scope) not in self.g3._denied)
        mids = tuple(r[0] for r in self.conn.execute("SELECT id FROM messages WHERE id>? AND id<=? "
              "AND json_extract(meta,'$.api_session')=? ORDER BY id LIMIT ?",
              (message_after, snapshot.message_head, snapshot.version_id, self.page_size)))
        message_next = mids[-1] if mids else snapshot.message_head
        page = LifecycleDeltaPage(snapshot.stamp, source_after, source_next, message_after, message_next,
                    visible, mids, (source_next, message_next) == (snapshot.source_head.position, snapshot.message_head))
        return self.page_hook(page) if self.page_hook else page

    def record(self):
        row = self.conn.execute('SELECT payload FROM lifecycle_candidate_state WHERE conversation_id=?', (CID,)).fetchone()
        return decode_record(row[0]) if row else None

    @contextmanager
    def transaction(self, cid):
        harness = self
        class Transaction:
            def load(self):
                return harness.record()

            def receipt(self, request_id):
                row = harness.conn.execute('SELECT payload FROM lifecycle_candidate_receipts WHERE conversation_id=? AND request_id=?',
                                            (cid, request_id)).fetchone()
                return LifecycleReceipt(**json.loads(row[0])) if row else None

            def save(self, expected_revision, state, receipt):
                old = self.load()
                if harness.save_conflict or (old.revision if old else 0) != expected_revision:
                    return False
                harness.conn.execute('INSERT OR REPLACE INTO lifecycle_candidate_state VALUES (?,?,?)',
                                      (cid, state.revision, encode(state)))
                harness.conn.execute('INSERT INTO lifecycle_candidate_receipts VALUES (?,?,?)',
                                      (cid, receipt.request_id, encode(receipt)))
                return True
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield Transaction()
            if self.fail_commit == 'before':
                self.fail_commit = None
                raise OSError('synthetic private commit failure')
            self.conn.commit()
            if self.fail_commit == 'after':
                self.fail_commit = None
                raise OSError('synthetic lost commit acknowledgement')
        except BaseException:
            self.conn.rollback()
            raise

    def read_receipt(self, digest, *, access):
        row = self.conn.execute('SELECT payload FROM fixture_window_receipts WHERE digest=?', (digest,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0]); value['source_cursor'] = SourceCursor(**value['source_cursor'])
        return WindowReceipt(**value)

    def confirm(self, *, status='confirmed', **changes):
        state = self.record()
        ack = WindowReceipt(CID, state.provider, state.version_id, state.window_id, state.target_window_id,
                 'run_' + str(state.context_epoch + 1), state.context_epoch + 1,
                 state.packet_sha256, state.source_cursor, state.message_cursor, status)
        ack = replace(ack, **changes)
        self.conn.execute('INSERT OR REPLACE INTO fixture_window_receipts VALUES (?,?)', (state.packet_sha256, encode(ack)))
        if status == 'confirmed' and not changes:
            runtime = self.get('runtime')
            runtime.update(window_id=ack.target_window_id, model_run_id=ack.model_run_id, epoch=ack.context_epoch,
                           observed_source=ack.source_cursor.position, observed_message=ack.message_cursor)
            self.put('runtime', runtime)
        return ack


def restart_worker(path):
    """Fresh interpreter, same synthetic canonical DB, no live entrypoint."""
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')):
        h = Harness(path, seed=False)
        result = h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        packet = h.engine.packet(CID, access=CTX)
        print(json.dumps({'phase': h.record().phase, 'replayed': result.replayed,
                          'conversation_id': packet.conversation_id, 'pending': list(packet.pending)}))
        h.conn.close()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'canonical.sqlite'
        self.h = Harness(self.path); self.addCleanup(lambda: self.h.conn.close())
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))

    def closeout(self, rid='close_1', **kwargs):
        return self.h.engine.closeout(CID, rid, window_id='window_0', context_epoch=0,
                                       active_state=ACTIVE, access=CTX, **kwargs)

    def prepare(self):
        self.closeout()
        return self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)

    def delta(self, rid='delta_1', **kwargs):
        state = self.h.record()
        return self.h.engine.reentry_delta(CID, rid, after_source=state.source_cursor,
                                             after_message=state.message_cursor, access=CTX, **kwargs)

    def activate(self, rid='activate_1'):
        return self.h.engine.activate(CID, rid, packet_sha256=self.h.record().packet_sha256, access=CTX)

    def rejects(self, fn, category):
        with self.assertRaises(LifecycleError) as caught:
            fn()
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), category)
        for private in ('synthetic_conversation', 'synthetic source text', str(self.path)):
            self.assertNotIn(private, str(caught.exception))

    def test_default_disabled_without_authority_or_repository_access(self):
        engine = SessionLifecycle(None, None, None, None)
        self.rejects(lambda: engine.closeout(CID, 'x', window_id='w', context_epoch=0, access=CTX), 'lifecycle_disabled')

    def test_snapshot_uses_existing_web_session_identifier_contract(self):
        cid = 'api.example.' + 'a' * 148  # Existing authority permits 160 chars and periods.
        snapshot = replace(self.h.snapshot(CID, CTX), conversation_id=cid, version_id=cid)
        self.assertIsNone(self.h.engine._snapshot(snapshot, cid))
        bad = replace(snapshot, conversation_id=cid+'a', version_id=cid+'a')
        self.rejects(lambda: self.h.engine._snapshot(bad, cid+'a'), 'lifecycle_invalid')

    def test_closeout_is_durable_idempotent_and_does_not_copy_messages(self):
        before = self.h.conn.execute('SELECT * FROM messages').fetchall()
        first = self.closeout(); second = self.closeout()
        self.assertFalse(first.replayed); self.assertTrue(second.replayed)
        self.assertEqual(self.h.record().revision, 1)
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(), before)
        self.assertEqual(len(self.h.session_authority.session_rows()), 1)
        self.assertEqual(self.h.record().closeout_checks, ('stream_idle','tool_idle','dispatch_known'))
        self.assertEqual(self.h.record().closeout_evidence_candidates, ('anchor_1','event_1'))

    def test_summary_is_only_a_derived_candidate_and_preserved_in_capsule(self):
        self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0,
                               summary_candidates=('synthetic unverified summary',), access=CTX)
        self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        packet = self.h.engine.packet(CID, access=CTX)
        self.assertEqual(packet.summary_candidates, ('synthetic unverified summary',))
        self.assertEqual(packet.authority, 'derived')
        self.assertEqual(self.h.g3._sources[('event_1',1)], b'synthetic source text')

    def test_same_request_with_changed_inputs_conflicts(self):
        self.closeout()
        self.rejects(lambda: self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0,
                     active_state=(), access=CTX), 'lifecycle_request_conflict')

    def test_safe_point_rejects_stream_tool_and_unknown_dispatch(self):
        for key in ('streaming', 'tool_inflight', 'uncertain_dispatch'):
            runtime = self.h.get('runtime'); runtime[key] = True; self.h.put('runtime', runtime)
            self.rejects(self.closeout, 'lifecycle_unsafe_boundary')
            self.assertIsNone(self.h.record())
            runtime[key] = False; self.h.put('runtime', runtime)

    def test_closeout_uses_consumed_cursors_then_reentry_catches_new_events(self):
        self.h.source('event_2', 'synthetic new evidence')
        mid = self.h.message()
        self.closeout()
        self.assertEqual((self.h.record().source_cursor.position, self.h.record().message_cursor), (2, 1))
        self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        packet = self.h.engine.packet(CID, access=CTX)
        self.assertEqual((packet.source_cursor.position, packet.message_cursor), (3, mid))
        self.assertEqual(packet.canonical_message_ids, (mid,))
        self.assertEqual((packet.conversation_id, packet.pending, packet.active_state), (CID, ('task_1',), ACTIVE))

    def test_reentry_prepared_is_not_window_or_reply_success(self):
        result = self.prepare()
        self.assertEqual(self.h.record().phase, 'prepared')
        self.assertEqual(self.h.record().window_id, 'window_0')
        self.assertFalse(result.window_confirmed); self.assertFalse(result.reply_completed)
        packet = self.h.engine.packet(CID, access=CTX)
        self.assertEqual(packet.authority, 'derived'); self.assertEqual(packet.anchors[0].layer, 'fixed')

    def test_unknown_and_failed_receipts_never_activate_or_resend(self):
        self.prepare()
        self.rejects(self.activate, 'lifecycle_confirmation_unknown')
        self.h.confirm(status='unknown')
        self.rejects(self.activate, 'lifecycle_confirmation_unknown')
        self.h.confirm(status='failed')
        self.rejects(self.activate, 'lifecycle_confirmation_failed')
        self.assertEqual((self.h.record().window_id, self.h.record().context_epoch), ('window_0', 0))

    def test_confirmed_receipt_preserves_conversation_and_advances_only_window_epoch(self):
        self.prepare(); self.h.confirm()
        result = self.activate()
        state = self.h.record()
        self.assertTrue(result.window_confirmed); self.assertFalse(result.reply_completed)
        self.assertEqual((state.conversation_id, state.window_id, state.context_epoch), (CID, 'window_1', 1))
        self.assertEqual(len(self.h.session_authority.session_rows()), 1)
        self.assertTrue(self.activate().replayed)

    def test_wrong_receipt_binding_and_reused_model_run_are_rejected(self):
        self.prepare()
        for changes in ({'provider':'codex'}, {'version_id':'other'}, {'context_epoch':99},
                        {'packet_sha256':'0'*64}, {'source_cursor':SourceCursor('wrong',2)},
                        {'model_run_id':'run_0'}, {'context_epoch':True}, {'message_cursor':True}):
            self.h.confirm(**changes)
            self.rejects(self.activate, 'lifecycle_confirmation_mismatch')

    def test_activation_lost_response_is_reconciled_without_reobserving_or_resending(self):
        self.prepare(); self.h.confirm(); self.h.fail_commit = 'after'
        self.rejects(self.activate, 'lifecycle_storage_unavailable')
        self.assertEqual(self.h.record().phase, 'active')
        with patch.object(self.h, 'read_receipt', side_effect=AssertionError('no repeated runtime action')):
            self.assertTrue(self.activate().replayed)

    def test_confirmed_receipt_without_matching_current_runtime_is_insufficient(self):
        self.prepare(); self.h.confirm()
        runtime = self.h.get('runtime')
        runtime.update(window_id='window_0', model_run_id='run_0', epoch=0)
        self.h.put('runtime', runtime)
        self.rejects(self.activate, 'lifecycle_confirmation_mismatch')
        self.assertEqual(self.h.record().phase, 'prepared')

    def test_competing_runtime_window_cannot_be_overwritten_by_historical_receipt(self):
        self.prepare(); self.h.confirm()
        runtime = self.h.get('runtime')
        runtime.update(window_id='foreign_window', model_run_id='foreign_run', epoch=7)
        self.h.put('runtime', runtime)
        self.rejects(self.activate, 'lifecycle_window_changed')
        self.assertEqual(self.h.record().window_id, 'window_0')

    def test_corrupt_durable_epoch_or_phase_fails_closed_before_receipt_replay(self):
        self.closeout(); original = self.h.record()
        for changes in ({'context_epoch':True}, {'phase':'unknown'}, {'message_cursor':-1}):
            self.h.conn.execute('UPDATE lifecycle_candidate_state SET payload=?', (encode(replace(original, **changes)),))
            self.rejects(self.closeout, 'lifecycle_state_invalid')
        self.h.conn.execute('UPDATE lifecycle_candidate_state SET payload=?', (encode(original),))

    def test_source_byte_tampering_does_not_serve_prepared_packet(self):
        self.prepare(); before = self.h.record()
        self.h.g3._wrong_bytes['event_1'] = b'synthetic changed byte'
        self.rejects(lambda: self.h.engine.packet(CID, access=CTX), 'lifecycle_evidence_unavailable')
        self.assertEqual(self.h.record(), before)

    def test_concurrent_identical_closeout_has_one_commit_and_one_replay(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.closeout(), range(2)))
        self.assertEqual(sum(r.replayed for r in results), 1)
        self.assertEqual(self.h.record().revision, 1)

    def test_new_message_requires_delta_before_packet_or_activation(self):
        self.prepare(); self.h.confirm()
        mid = self.h.message()
        self.rejects(lambda: self.h.engine.packet(CID, access=CTX), 'lifecycle_delta_required')
        self.rejects(self.activate, 'lifecycle_delta_required')
        self.delta()
        self.assertEqual(self.h.engine.packet(CID, access=CTX).canonical_message_ids, (mid,))
        self.rejects(self.activate, 'lifecycle_confirmation_unknown')

    def test_delta_retry_does_not_count_or_include_messages_twice(self):
        self.prepare(); mid = self.h.message(); old = self.h.record()
        first = self.delta()
        second = self.h.engine.reentry_delta(CID, 'delta_1', after_source=old.source_cursor,
                                             after_message=old.message_cursor, access=CTX)
        self.assertTrue(second.replayed)
        self.assertEqual(first.new_message_refs, 1)
        self.assertEqual(self.h.record().message_refs, (mid,))
        self.assertEqual(self.h.record().revision, 3)

    def test_stale_delta_cursor_with_new_request_id_is_rejected(self):
        self.prepare(); old = self.h.record(); self.h.message(); self.delta()
        self.rejects(lambda: self.h.engine.reentry_delta(CID, 'different_delta', after_source=old.source_cursor,
                         after_message=old.message_cursor, access=CTX), 'lifecycle_delta_cursor_conflict')

    def test_delta_page_failure_rolls_back_state_and_receipt(self):
        self.prepare(); old = self.h.record()
        self.h.source('event_2', 'synthetic 2'); self.h.source('event_3', 'synthetic 3')
        def fail_second(page):
            if page.source_after == 3:
                raise OSError('synthetic network interruption')
            return page
        self.h.page_hook = fail_second
        self.rejects(self.delta, 'lifecycle_storage_unavailable')
        self.assertEqual(self.h.record(), old)
        self.h.page_hook = None
        self.assertEqual(self.delta().source_changes, 2)

    def test_duplicate_skipped_reordered_and_boolean_delta_pages_rejected(self):
        self.prepare(); self.h.message(); self.h.source('event_2', 'synthetic 2')
        for changes in ({'message_after':0}, {'source_after':0}, {'message_next':True},
                        {'message_ids':(2,2)}, {'source_next':2, 'message_next':1, 'source_events':(), 'message_ids':(), 'done':False},
                        {'stamp':'wrong'}, {'done':False}):
            self.h.page_hook = lambda page: replace(page, **changes)
            self.rejects(self.delta, 'lifecycle_delta_invalid')
        self.assertEqual(self.h.record().revision, 2)

    def test_source_change_while_preparing_packet_prevents_commit(self):
        self.closeout()
        self.h.validation_hook = lambda: self.h.source('event_2', 'synthetic changed')
        self.rejects(lambda: self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1',
                     target_window_id='window_1', access=CTX), 'lifecycle_authority_changed')
        self.assertEqual(self.h.record().phase, 'closed')

    def test_pending_cannot_disappear_without_durable_settlement(self):
        self.prepare()
        runtime = self.h.get('runtime'); runtime['pending'] = []; self.h.put('runtime', runtime)
        self.rejects(self.delta, 'lifecycle_pending_unaccounted')
        runtime['settled'] = ['task_1']; self.h.put('runtime', runtime)
        self.delta()
        self.assertEqual(self.h.record().pending, ())

    def test_deleted_session_and_other_principal_are_rejected_before_replay(self):
        self.closeout()
        other = AccessScope('other_owner', CTX.allowed_scopes)
        self.rejects(lambda: self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0,
                     active_state=ACTIVE, access=other), 'lifecycle_access_denied')
        config = self.h.get('legacy'); config['sessions'] = []
        config['deleted_sessions'] = [{'id':CID,'provider':'api','deleted_at':'2026-11-04T01:00:00Z'}]
        self.h.put('legacy', config)
        self.rejects(self.closeout, 'lifecycle_session_unavailable')

    def test_changed_provider_or_selected_version_blocks_existing_closeout(self):
        self.closeout()
        config = self.h.get('legacy'); config['sessions'][0]['provider'] = 'codex'; self.h.put('legacy', config)
        self.rejects(lambda: self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1',
                     target_window_id='window_1', access=CTX), 'lifecycle_session_changed')
        config['sessions'][0]['provider'] = 'api'
        config['sessions'].append({**config['sessions'][0], 'id':'synthetic_version'})
        self.h.put('legacy', config)
        self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)', ('synthetic_version',CID,CID,'regenerate',1,1,1))
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)', (CID,'synthetic_version'))
        runtime = self.h.get('runtime'); runtime['observed_message'] = 0; self.h.put('runtime', runtime)
        self.rejects(lambda: self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1',
                     target_window_id='window_1', access=CTX), 'lifecycle_session_changed')

    def test_commit_failure_before_commit_is_retryable_without_partial_state(self):
        self.h.fail_commit = 'before'
        self.rejects(self.closeout, 'lifecycle_storage_unavailable')
        self.assertIsNone(self.h.record())
        self.assertFalse(self.closeout().replayed)

    def test_lost_commit_response_reconciles_by_same_request_without_repeating(self):
        self.h.fail_commit = 'after'
        self.rejects(self.closeout, 'lifecycle_storage_unavailable')
        self.assertEqual(self.h.record().phase, 'closed')
        self.assertTrue(self.closeout().replayed)
        self.assertEqual(self.h.record().revision, 1)

    def test_repository_compare_and_swap_conflict_rolls_back(self):
        self.h.save_conflict = True
        self.rejects(self.closeout, 'lifecycle_commit_conflict')
        self.assertIsNone(self.h.record())

    def test_reentry_budget_is_bytes_not_claimed_tokens_and_never_truncates(self):
        self.closeout()
        self.rejects(lambda: self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1',
                    target_window_id='window_1', packet_budget_bytes=10, access=CTX), 'lifecycle_budget_exceeded')
        self.assertEqual(self.h.record().phase, 'closed')

    def test_source_revision_and_acl_changes_require_reverified_packet(self):
        self.prepare()
        self.h.source('event_1', 'synthetic revised', revision=2)
        self.rejects(lambda: self.h.engine.packet(CID, access=CTX), 'lifecycle_delta_required')
        self.delta()
        self.assertEqual(self.h.engine.packet(CID, access=CTX).recent[0].source_refs[0].revision, 2)
        self.h.g3.deny(CTX.principal_id, 'private_A')
        self.rejects(lambda: self.h.engine.packet(CID, access=CTX), 'lifecycle_delta_required')
        self.delta('delta_2')
        self.assertEqual(self.h.engine.packet(CID, access=CTX).recent, ())

    def test_source_generation_and_cursor_rollback_are_rejected(self):
        self.prepare()
        runtime = self.h.get('runtime'); runtime['generation'] = 'different'; self.h.put('runtime', runtime)
        self.rejects(self.delta, 'lifecycle_cursor_invalid')

    def test_restart_in_fresh_process_preserves_pending_receipts_and_same_conversation(self):
        self.closeout(); self.h.conn.close()
        command = [sys.executable, '-c', 'import sys; from backend.tests.test_session_lifecycle import restart_worker; restart_worker(sys.argv[1])', str(self.path)]
        first = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
        second = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
        self.h = Harness(self.path, seed=False)
        self.assertFalse(json.loads(first.stdout)['replayed']); self.assertTrue(json.loads(second.stdout)['replayed'])
        self.assertEqual(self.h.record().phase, 'prepared')
        self.assertEqual(json.loads(second.stdout)['conversation_id'], CID)
        self.assertEqual(json.loads(second.stdout)['pending'], ['task_1'])

    def test_api_and_codex_synthetic_paths_use_same_protocol_but_real_paths_remain_uninstalled(self):
        self.prepare(); self.h.confirm(); self.activate()
        self.h.conn.close()
        self.h = Harness(self.path.parent/'codex.sqlite', provider='codex')
        self.prepare(); self.h.confirm(); self.activate()
        self.assertEqual(self.h.record().provider, 'codex')
        self.assertEqual(self.h.record().conversation_id, CID)

    def test_unsupported_provider_path_is_explicit(self):
        self.h.engine.supported = frozenset({'codex'})
        self.rejects(self.closeout, 'lifecycle_provider_unsupported')

    def test_historical_closeout_receipt_does_not_rewind_active_state(self):
        self.prepare(); self.h.confirm(); self.activate()
        result = self.closeout()
        self.assertTrue(result.replayed); self.assertEqual(result.phase_at_apply, 'closed')
        self.assertEqual(self.h.record().phase, 'active')
        self.assertFalse(result.reply_completed)

    def test_second_closeout_keeps_logical_id_and_forbids_reused_window(self):
        self.prepare(); self.h.confirm(); self.activate()
        self.h.engine.closeout(CID, 'close_2', window_id='window_1', context_epoch=1, access=CTX)
        self.rejects(lambda: self.h.engine.reentry(CID, 'prepare_2', closeout_id='close_2',
                     target_window_id='window_0', access=CTX), 'lifecycle_window_reused')

    def test_no_source_text_in_durable_record_or_new_canonical_messages(self):
        self.prepare()
        stored = self.h.conn.execute('SELECT payload FROM lifecycle_candidate_state').fetchone()[0]
        self.assertNotIn('synthetic source text', stored)
        self.assertNotIn('synthetic reviewed anchor', stored)
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.h.get('legacy')['api_session'], CID)

    def test_module_has_no_db_paths_runtime_sends_model_or_environment_reads(self):
        from backend import session_lifecycle as module
        source = inspect.getsource(module)
        for forbidden in ('sqlite3.connect', 'os.environ', 'getenv(', 'httpx', 'requests.',
                          'openai', 'create_web_session', 'create_api_session', 'send_message'):
            self.assertNotIn(forbidden, source)
        self.assertEqual({k for k in vars(module.LifecycleRuntime) if not k.startswith('_')}, {'read_receipt'})


if __name__ == '__main__':
    unittest.main()
