import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import web_message_versions as versions
from backend.usage_ledger import Ledger
from backend.usage_ledger_report import build_report
from backend.web_session_fork_routes import install
from backend.web_session_provider_authority import WebSessionProviderAuthority


class RewriteTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = str(Path(root.name) / "relay.db")
        self.source = "前缀保密\r\n同一句🙂\r\n同一句🙂\r\n后缀保密"
        self.prefix = "前缀保密\r\n同一句🙂\r\n"
        self.config = {"sessions": [
            {"id": "source", "title": "聊天", "provider": "api"},
            {"id": "other", "title": "另一窗", "provider": "api"},
            {"id": "native", "title": "订阅", "provider": "codex"},
        ], "active_session": "source"}
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            rows = [
                (1, "in", "user", "旧提问", "source"), (2, "out", "reply", self.source, "source"),
                (3, "in", "user", "后续对话", "source"), (4, "out", "reply", self.source, "native"),
            ]
            for mid, direction, kind, text, sid in rows:
                meta = {"api_session": sid, "api": {"usage": {"total_tokens": 10}}}
                conn.execute("INSERT INTO messages VALUES (?,'2026-11-12T01:00:00Z',?,?,?,?)",
                             (mid, direction, kind, text, json.dumps(meta)))
        self.model = AsyncMock(return_value={"outcome": "success", "text": "替换句"})
        def auth(request):
            if request.headers.get("x-api-loop-internal-token") != "synthetic":
                raise HTTPException(401)
        legacy = SimpleNamespace(RELAY_DB=self.path, run_model=self.model, check_internal_auth=auth,
                                 read_internal_json=lambda request: request.json(),
                                 load_config=lambda: self.config.copy(), save_config=self.set_config,
                                 now_iso=lambda: "2026-11-12T02:00:00Z")
        self.authority = WebSessionProviderAuthority(legacy)
        app = FastAPI()
        install(app, legacy, self.authority, RLock(), rewrite_runtime=SimpleNamespace(usage_collector=None))
        self.client = TestClient(app, headers={"X-API-Loop-Internal-Token": "synthetic"})
        self.addCleanup(self.client.close)

    def set_config(self, value):
        self.config = value

    def command(self, **kwargs):
        first = len(self.prefix.encode("utf-16-le")) // 2
        return {"request_id": str(uuid.uuid4()), "message_id": 2, "start": first, "end": first + 5,
                "source_sha256": hashlib.sha256(self.source.encode()).hexdigest(), **kwargs}

    def post(self, op, body, sid="source"):
        return self.client.post(f"/loop/sessions/{sid}/rewrite/{op}", json=body)

    def test_model_receives_only_selection_and_preview_never_writes_history(self):
        body = self.command(instructions="请简洁一点")
        response = self.post("preview", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["replacement"], "替换句")
        args = self.model.await_args
        self.assertEqual(json.loads(args.args[0][1]["content"]), {
            "editing_request": "请简洁一点", "selected_passage": "同一句🙂",
        })
        self.assertNotIn("前缀保密", str(args.args))
        self.assertFalse(args.kwargs["allow_fallback"])
        self.assertEqual(self.post("preview", body).status_code, 409)
        self.model.assert_awaited_once()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone(), (4,))
        self.assertEqual(versions.public_state(self.authority, relay_db=self.path)["versions"], [])

    def test_save_forks_the_verified_range_and_preserves_source_and_selection(self):
        body = self.command(replacement="替换句")
        result = self.post("versions", body)
        self.assertEqual(result.status_code, 200, result.text)
        target = result.json()["created"]["id"]
        self.assertTrue(self.post("versions", body).json()["duplicate"])
        self.assertEqual(self.post("versions", {**body, "replacement": "另一次"}).status_code, 409)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT text,meta FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id", (target,)).fetchall()
            self.assertEqual(conn.execute("SELECT text FROM messages WHERE id=2").fetchone()[0], self.source)
            self.assertEqual(conn.execute("SELECT text FROM messages WHERE id=3").fetchone()[0], "后续对话")
        self.assertEqual([r[0] for r in rows], ["旧提问", self.prefix + "替换句\r\n后缀保密"])
        self.assertIsNone(json.loads(rows[-1][1])["version_usage"])
        self.assertEqual(json.loads(rows[-1][1])["version_key"], 2)
        state = versions.public_state(self.authority, relay_db=self.path)
        self.assertEqual(state["versions"][0]["point_key"], 2)
        self.assertEqual(state["versions"][0]["mode"], "regenerate")
        self.assertEqual(state["selections"], [])
        self.model.assert_not_awaited()

    def test_stale_or_foreign_or_split_ranges_and_codex_fail_before_dispatch(self):
        body = self.command(instructions="")
        cases = [
            ("source", {**body, "source_sha256": "0" * 64}, 409),
            ("source", {**body, "end": body["end"] - 1}, 400),
            ("other", body, 404),
            ("native", {**body, "message_id": 4}, 409),
        ]
        for sid, payload, status in cases:
            self.assertEqual(self.post("preview", payload, sid).status_code, status)
        self.assertEqual(self.post("versions", self.command(replacement="改", source_sha256="0" * 64)).status_code, 409)
        self.assertEqual(self.client.post("/loop/sessions/source/rewrite/preview", json=body,
                                          headers={"X-API-Loop-Internal-Token": "bad"}).status_code, 401)
        self.model.assert_not_awaited()

    def test_changed_reply_after_dispatch_withdraws_preview(self):
        async def change(*args, **kwargs):
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE messages SET text='另一份回复' WHERE id=2")
            return {"outcome": "success", "text": "替换句"}
        self.model.side_effect = change
        self.assertEqual(self.post("preview", self.command(instructions="")).json()["error"], "rewrite_source_changed")
        self.model.assert_awaited_once()

    def test_rewrite_request_counts_for_consumption_without_advancing_chat_stats(self):
        ledger = Ledger(self.path + ".usage")
        base = {"mode": "api", "provider_id": "synthetic", "session_id": "source", "model": "first",
                "status": "completed", "usage_complete": True, "dialect": "openai_chat"}
        ledger.append({**base, "request_id": "chat-1", "turn_id": "api-gen-1",
                       "at": "2026-11-12T01:00:00Z", "raw_usage": {"prompt_tokens": 100, "completion_tokens": 10},
                       "context": {"tokens": 100, "limit": 1000, "stale": False, "source": "synthetic"}})
        ledger.append({**base, "request_id": "rewrite-request-1", "turn_id": "rewrite-preview-1", "model": "second",
                       "at": "2026-11-12T02:00:00Z", "raw_usage": {"prompt_tokens": 20, "completion_tokens": 5}})
        report = build_report(ledger, "api", "source")
        self.assertEqual(report["summary"]["requests"], 2)
        self.assertEqual(report["summary"]["observed_turns"], 1)
        self.assertEqual(report["summary"]["metrics"]["input"]["value"], 120)
        self.assertEqual(report["last_round"]["metrics"]["input"]["value"], 100)
        self.assertEqual(report["context"]["tokens"], 100)
        self.assertIs(report["context"]["stale"], False)
        self.assertEqual(report["configuration_changes"], [])
        self.assertEqual(report["recent_requests"][-1]["turn_id"], "rewrite-preview-1")
