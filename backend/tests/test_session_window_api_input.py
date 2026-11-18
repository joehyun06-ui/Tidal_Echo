"""API reentry input with exact canonical messages and checked G3 excerpts."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import api_web_generation, session_window_api_input
from backend.session_lifecycle import LifecycleError, _json
from backend.session_window_api_input import FORMAT, prepare_api_window_input
from backend.tests.test_session_lifecycle import ACTIVE, CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness


WIRE_CHECK = r"""
import asyncio, hashlib, json, socket, sys
from pathlib import Path
from unittest.mock import patch
import httpx

def blocked(*args, **kwargs):
    raise AssertionError('isolated request assembly')

read_text = Path.read_text

def read_fixture(path, *args, **kwargs):
    if path.name == '.env':
        raise FileNotFoundError()
    return read_text(path, *args, **kwargs)

with (
    patch.object(Path, 'read_text', read_fixture), patch.object(socket.socket, 'connect', blocked),
    patch.object(socket.socket, 'connect_ex', blocked), patch('socket.getaddrinfo', blocked),
):
    from examples import api_loop
    expected = json.load(sys.stdin)
    seen = []

    def receive(request):
        data = json.loads(request.content)
        path = request.url.path
        assert request.method == 'POST'
        assert request.url.host == 'synthetic.invalid'
        seen.append(path)
        if path == '/v1/chat/completions':
            assert data['messages'] == [expected]
            return httpx.Response(200, json={'choices': [{'message': {'content': 'synthetic reply'}}]})
        assert path == '/v1/responses'
        assert data['input'] == [expected]
        return httpx.Response(200, json={'status':'completed', 'output_text': 'synthetic reply'})

    async def check():
        for model in ('fixture-chat', 'gpt-5.6-sol'):
            route = {'url': 'https://synthetic.invalid/v1', 'key': 'synthetic', 'model': model}
            result = await api_loop._complete_provider(route, [expected])
            assert result['text'] == 'synthetic reply'

    def client(**options):
        return httpx.AsyncClient(transport=httpx.MockTransport(receive), **options)

    with patch.object(api_loop, '_provider_client', client):
        asyncio.run(check())
    assert seen == ['/v1/chat/completions', '/v1/responses']
    print(hashlib.sha256(expected['content'].encode()).hexdigest())
"""


@unittest.skipUnless(os.name == "posix", "canonical private JSON fixture uses POSIX")
class ApiWindowInputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.harness = FileHarness(self.root / "canonical.sqlite")
        self.addCleanup(self.harness.conn.close)
        self.engine = self.harness.engine

    def append(self, *, text, session=CID, kind="user", direction="in", **meta):
        row = self.harness.conn.execute(
            "INSERT INTO messages(ts,kind,direction,text,meta) VALUES (?,?,?,?,?)",
            ("2026-11-18T00:00:00Z", kind, direction, text, json.dumps({"api_session": session, **meta})),
        )
        return row.lastrowid

    def stage(self):
        self.engine.closeout(CID, "close_1", window_id="window_0", context_epoch=0, active_state=ACTIVE, access=CTX)
        self.engine.reentry(CID, "prepare_1", closeout_id="close_1", target_window_id="window_1", access=CTX)
        return self.harness.record()

    def prepare(self, **options):
        return prepare_api_window_input(self.engine, CID, access=CTX, enabled=True, **options)

    def rejects(self, action, category):
        with self.assertRaises(LifecycleError) as caught:
            action()
        self.assertEqual(caught.exception.category, category)

    def test_default_off_ignores_unusable_dependencies(self):
        self.rejects(lambda: prepare_api_window_input(object(), object(), access=object()), "lifecycle_disabled")

    def test_materializes_exact_scoped_ordered_history_and_verified_g3(self):
        earlier = self.append(text="  合成消息")
        self.append(text="other session fixture", session="other_conversation")
        later = self.append(text="synthetic reply ", kind="reply", direction="out", attachments=[])
        state = self.stage()
        before = list(self.harness.conn.iterdump())
        config = self.harness.config_path.read_bytes()
        item = self.prepare()
        payload = json.loads(item.content)
        self.assertEqual((item.conversation_id, item.version_id, item.target_window_id, item.context_epoch),
                         (CID, CID, "window_1", 1))
        self.assertEqual((item.packet_sha256, item.source_cursor, item.message_cursor),
                         (state.packet_sha256, state.source_cursor, state.message_cursor))
        self.assertEqual(payload["format"], FORMAT)
        self.assertEqual(payload["authority"], "derived")
        self.assertEqual(payload["canonical_messages"], [
            {"id": earlier, "role": "user", "text": "  合成消息"},
            {"id": later, "role": "assistant", "text": "synthetic reply "},
        ])
        self.assertEqual([(item["event_id"], item["revision"], item["excerpt"]) for item in payload["g3_excerpts"]],
                         [("anchor_1", 1, "synthetic reviewed anchor"), ("event_1", 1, "synthetic source text")])
        self.assertEqual(payload["pending"], ["task_1"])
        self.assertEqual(payload["active_state"], dict(ACTIVE))
        self.assertEqual(item.as_message()["role"], "user")
        self.assertNotIn("合成消息", repr(item))
        self.assertEqual(item.input_sha256, hashlib.sha256(_json([FORMAT, item.packet_sha256, "user", item.content])).hexdigest())
        self.assertEqual(self.prepare(), item)
        self.assertEqual(list(self.harness.conn.iterdump()), before)
        self.assertEqual(self.harness.config_path.read_bytes(), config)

    def test_api_loop_transports_keep_the_same_user_context(self):
        self.append(text="synthetic input")
        self.stage()
        message = self.prepare().as_message()
        env = {
            "PATH": os.defpath, "HOME": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "RELAY_DB": self.harness.path, "LOOP_CONFIG": str(self.harness.config_path),
            "API_LOOP_INTERNAL_TOKEN": "synthetic-api-input-test-token-32chars",
        }
        result = subprocess.run([sys.executable, "-B", "-c", WIRE_CHECK], cwd=self.root,
                                env=env, input=json.dumps(message), capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(message["content"].encode()).hexdigest())

    def test_selected_version_keeps_other_versions_out_of_the_input(self):
        child = "synthetic_child"
        config = self.harness.get("legacy")
        config["sessions"].append({**config["sessions"][0], "id": child})
        self.harness.put("legacy", config)
        self.harness.conn.execute("INSERT INTO web_message_versions(version_id,root_id) VALUES (?,?)", (child, CID))
        self.harness.conn.execute("INSERT INTO web_version_selections VALUES (?,?)", (CID, child))
        cursor = self.append(text="previous child message", session=child)
        self.harness.put("runtime", {**self.harness.get("runtime"), "observed_message": cursor})
        self.append(text="root message")
        included = self.append(text="child message", session=child)
        self.stage()
        item = self.prepare()
        self.assertEqual((item.conversation_id, item.version_id), (CID, child))
        self.assertEqual(json.loads(item.content)["canonical_messages"],
                         [{"id": included, "role": "user", "text": "child message"}])

    def test_input_hash_binds_canonical_bytes_at_same_packet_reference(self):
        mid = self.append(text="synthetic original")
        self.stage()
        original = self.prepare()
        self.harness.conn.execute("UPDATE messages SET text=? WHERE id=?", ("synthetic changed", mid))
        changed = self.prepare()
        self.assertEqual(changed.packet_sha256, original.packet_sha256)
        self.assertNotEqual(changed.input_sha256, original.input_sha256)

    def test_rejects_media_or_blank_text_instead_of_skipping_a_message(self):
        mid = self.append(text="caption", attachments=[{"kind": "image", "name": "synthetic"}])
        self.stage()
        self.rejects(self.prepare, "lifecycle_input_media_unsupported")
        self.harness.conn.execute("UPDATE messages SET meta=?,text=' ' WHERE id=?", (json.dumps({"api_session": CID}), mid))
        self.rejects(self.prepare, "lifecycle_input_message_unavailable")

    def test_rejects_over_budget_without_truncating_material(self):
        self.append(text="轮次" * 200)
        self.stage()
        self.rejects(lambda: self.prepare(max_bytes=400), "lifecycle_input_budget_exceeded")
        self.assertGreater(len(self.prepare().content.encode()), 1000)

    def test_rejects_a_newer_canonical_row_even_with_stale_runtime_head(self):
        state = self.stage()
        self.append(text="new unseen message")
        self.harness.snapshot_hook = lambda snapshot: replace(snapshot, message_head=state.message_cursor)
        self.rejects(self.prepare, "lifecycle_delta_required")

    def test_normal_api_admission_advances_head_before_provider_dispatch(self):
        state = self.stage()
        store = api_web_generation.Store(self.harness.path)
        store.initialize()
        accepted = store.accept(CID, "synthetic ordinary input", {
            "api_session": CID, "channel": "web", "source": "relay",
        })
        self.assertGreater(accepted["id"], state.message_cursor)
        self.assertEqual(store.get(CID, accepted["id"])["status"], "queued")
        self.rejects(self.prepare, "lifecycle_delta_required")

    def test_rejects_g3_revocation_during_materialization(self):
        self.stage()
        original = session_window_api_input._canonical_messages

        def revoke(conn, packet, budget):
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            messages = original(conn, packet, budget)
            self.harness.g3.deny(CTX.principal_id, "private_A")
            return messages

        with patch.object(session_window_api_input, "_canonical_messages", side_effect=revoke):
            self.rejects(self.prepare, "lifecycle_authority_changed")

    def test_codex_prepared_packet_cannot_enter_api_context_path(self):
        config = self.harness.get("legacy")
        config["sessions"][0]["provider"] = "codex"
        self.harness.put("legacy", config)
        self.stage()
        self.rejects(self.prepare, "lifecycle_provider_unsupported")
