from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import os
import socket
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from backend import channel_store
from backend.tests._support import FAKE_CHAT, FAKE_USER, NoNetworkMixin, load_app, request, update, webhook_headers


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.payload


class TelegramReliabilityTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)

    async def queued_job(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        job = self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, job["id"])
        self.module.channel_store.finish_generation_dispatch(self.module.DB_PATH, job["id"], "awaiting_reply")
        return job

    @staticmethod
    def payload(job, text="answer"):
        return {"type": "reply", "text": text, "channel": "telegram",
                "channel_account": job["external_account_id"],
                "channel_conversation": job["external_conversation_id"],
                "api_session": job["api_session"], "generation_id": job["generation_id"],
                "stream_id": job["stream_id"], "reply_to": job["reply_to"]}

    async def test_atomic_completion_and_duplicate_route(self):
        job = await self.queued_job(); auth = {"Authorization": "Bearer test-relay-secret"}
        first = await request(self.module, "POST", "/channel/out", json=self.payload(job), headers=auth)
        second = await request(self.module, "POST", "/channel/out", json=self.payload(job), headers=auth)
        self.assertEqual(first.status_code, 200); self.assertTrue(second.json()["duplicate"])
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM telegram_completions").fetchone()[0], 1)

    async def test_delivery_insert_failure_rolls_back_message_and_job(self):
        job = await self.queued_job()
        with self.module.db() as conn:
            conn.execute("CREATE TRIGGER fail_delivery BEFORE INSERT ON delivery_attempts BEGIN SELECT RAISE(ABORT,'test'); END")
            conn.commit()
        meta = {k: v for k, v in self.payload(job).items() if k not in {"type", "text"}}
        with self.assertRaises(sqlite3.IntegrityError):
            self.module.channel_store.complete_telegram_generation(self.module.DB_PATH, meta=meta, text="answer")
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "awaiting_reply")
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 0)

    async def test_strong_correlation_mismatch_only_audits(self):
        job = await self.queued_job(); payload = self.payload(job); payload["channel_account"] = "wrong"
        response = await request(self.module, "POST", "/channel/out", json=payload,
                                 headers={"Authorization": "Bearer test-relay-secret"})
        self.assertEqual(response.status_code, 409)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "awaiting_reply")
            self.assertEqual(conn.execute("SELECT count(*) FROM channel_audit_events WHERE event_type='reply_uncorrelated'").fetchone()[0], 1)

    async def test_concurrent_duplicate_update_is_single_cost(self):
        responses, broadcasts = await self.concurrent_webhooks(update(), update())
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(broadcasts, 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM external_messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    async def test_concurrent_distinct_update_same_message_is_idempotent(self):
        responses, broadcasts = await self.concurrent_webhooks(
            update(update_id=1, message_id=10), update(update_id=2, message_id=10))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(broadcasts, 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM external_messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0], 1)

    async def concurrent_webhooks(self, first_body, second_body):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        broadcast_count = 0
        original_enqueue = self.module.channel_store.enqueue_telegram_update
        original_broadcast = self.module.broadcast

        def synchronized_enqueue(*args, **kwargs):
            barrier.wait(timeout=2)
            return original_enqueue(*args, **kwargs)

        async def counted_broadcast(subs, payload):
            nonlocal broadcast_count
            if isinstance(payload, dict) and "id" in payload:
                with lock:
                    broadcast_count += 1
            return await original_broadcast(subs, payload)

        def send(body):
            return asyncio.run(request(self.module, "POST", "/integrations/telegram/webhook",
                                       json=body, headers=webhook_headers()))

        with mock.patch.object(self.module.channel_store, "enqueue_telegram_update", new=synchronized_enqueue), \
             mock.patch.object(self.module, "broadcast", new=counted_broadcast), ThreadPoolExecutor(max_workers=2) as pool:
            loop = asyncio.get_running_loop()
            responses = await asyncio.gather(loop.run_in_executor(pool, send, first_body),
                                             loop.run_in_executor(pool, send, second_body))
        return responses, broadcast_count

    async def test_dispatch_outcomes_and_restart_recovery(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        async def uncertain(job, message):
            raise self.module.LoopDispatchError("timeout", True)
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, uncertain)
        await worker.run_generation_once()
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "dispatch_uncertain")
        self.assertFalse(await worker.run_generation_once())

        await request(self.module, "POST", "/integrations/telegram/webhook",
                      json=update(update_id=2, message_id=11), headers=webhook_headers())
        job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, job["id"])
        self.assertEqual(self.module.channel_store.recover_inflight_generations(self.module.DB_PATH), 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs WHERE id=?", (job["id"],)).fetchone()[0], "dispatch_uncertain")

    async def test_uncertain_dispatch_is_never_reclaimed_and_late_callback_completes(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        job = self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, job["id"])
        self.module.channel_store.finish_generation_dispatch(self.module.DB_PATH, job["id"], "dispatch_uncertain", "timeout")
        self.assertIsNone(self.module.channel_store.claim_generation_job(self.module.DB_PATH))
        response = await request(self.module, "POST", "/channel/out", json=self.payload(job),
                                 headers={"Authorization": "Bearer test-relay-secret"})
        self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "completed")

    async def test_worker_never_dispatches_awaiting_or_uncertain_job(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        job = self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, job["id"])
        calls = []
        async def dispatcher(*args): calls.append(args)
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, dispatcher)
        self.module.channel_store.finish_generation_dispatch(self.module.DB_PATH, job["id"], "awaiting_reply")
        self.assertFalse(await worker.run_generation_once())
        with self.module.db() as conn:
            conn.execute("UPDATE generation_jobs SET status='dispatch_uncertain' WHERE id=?", (job["id"],)); conn.commit()
        self.assertFalse(await worker.run_generation_once())
        self.assertEqual(calls, [])

    async def test_generation_attempt_limit(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        first = self.module.channel_store.claim_generation_job(self.module.DB_PATH, max_attempts=1)
        with self.module.db() as conn:
            conn.execute("UPDATE generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (first["id"],)); conn.commit()
        self.assertIsNone(self.module.channel_store.claim_generation_job(self.module.DB_PATH, max_attempts=1))
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "failed")

    async def test_webhook_strict_ids_bot_and_body_limit(self):
        for bad in (True, 1.5, -1, 2**63, "01"):
            body = update(); body["message"]["from"]["id"] = bad
            response = await request(self.module, "POST", "/integrations/telegram/webhook", json=body, headers=webhook_headers())
            self.assertEqual(response.json()["reason"], "malformed_update")
        body = update(); body["message"]["from"]["is_bot"] = True
        response = await request(self.module, "POST", "/integrations/telegram/webhook", json=body, headers=webhook_headers())
        self.assertEqual(response.json()["reason"], "bot_sender")
        oversized = b"{" + b"x" * (self.module.TELEGRAM.webhook_max_body_bytes + 1)
        response = await request(self.module, "POST", "/integrations/telegram/webhook", content=oversized, headers=webhook_headers())
        self.assertEqual((response.status_code, response.json()["error"]), (413, "body_too_large"))

    async def test_delivery_part_progress_and_error_classes(self):
        job = await self.queued_job(); text = "a" * 4096 + "b"
        self.module.channel_store.complete_telegram_generation(
            self.module.DB_PATH, meta={k: v for k, v in self.payload(job).items() if k not in {"type", "text"}}, text=text)
        calls = 0
        def opener(req, timeout):
            nonlocal calls
            calls += 1
            if calls == 1: return FakeResponse({"ok": True, "result": {"message_id": 9001}})
            raise TimeoutError()
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, lambda *_: None,
            self.module.telegram_integration.TelegramClient(self.module.TELEGRAM, opener=opener))
        await worker.run_delivery_once(); await worker.run_delivery_once()
        with self.module.db() as conn:
            parts = conn.execute("SELECT status,telegram_message_id FROM delivery_parts ORDER BY part_index").fetchall()
            mapping = conn.execute("SELECT external_message_id FROM external_messages WHERE direction='out'").fetchone()
        self.assertEqual((parts[0]["status"], parts[0]["telegram_message_id"]), ("delivered", "9001"))
        self.assertEqual(parts[1]["status"], "delivery_uncertain"); self.assertEqual(mapping[0], "9001")
        self.assertFalse(await worker.run_delivery_once())

    async def test_telegram_ok_false_429_and_missing_id(self):
        client = self.module.telegram_integration.TelegramClient
        reject = client(self.module.TELEGRAM, opener=lambda *a, **k: FakeResponse({"ok": False}))
        with self.assertRaises(self.module.telegram_integration.TelegramDeliveryError) as caught:
            reject.send_part(FAKE_CHAT, "x")
        self.assertFalse(caught.exception.uncertain)

        payload = io.BytesIO(json.dumps({"parameters": {"retry_after": 12}}).encode())
        def limited(req, timeout): raise urllib.error.HTTPError(req.full_url, 429, "limited", {}, payload)
        with self.assertRaises(self.module.telegram_integration.TelegramDeliveryError) as caught:
            client(self.module.TELEGRAM, opener=limited).send_part(FAKE_CHAT, "x")
        self.assertEqual(caught.exception.retry_after, 12)

        missing = client(self.module.TELEGRAM, opener=lambda *a, **k: FakeResponse({"ok": True, "result": {}}))
        with self.assertRaises(self.module.telegram_integration.TelegramDeliveryError) as caught:
            missing.send_part(FAKE_CHAT, "x")
        self.assertTrue(caught.exception.uncertain)

    async def test_hmac_audit_identifier_properties(self):
        one = channel_store.audit_id("secret-a", "telegram", "bot", "123")
        self.assertEqual(one, channel_store.audit_id("secret-a", "telegram", "bot", "123"))
        self.assertNotEqual(one, channel_store.audit_id("secret-a", "telegram", "bot", "124"))
        self.assertNotEqual(one, channel_store.audit_id("secret-b", "telegram", "bot", "123"))
        self.assertNotIn("123", one)

    async def test_loop_ack_validation_and_uncertain_timeout(self):
        routing = {"generation_id": "g", "stream_id": "s", "api_session": "a"}
        success = {"ok": True, "callback_delivered": True, **routing}
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=FakeResponse(success)):
            self.assertTrue(self.module._forward_to_loop_sync({"id": 1, "text": "x", "meta": {}}, routing)["ok"])
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=FakeResponse({"ok": False})):
            with self.assertRaises(self.module.LoopDispatchError) as caught:
                self.module._forward_to_loop_sync({"id": 1, "text": "x", "meta": {}}, routing)
            self.assertFalse(caught.exception.uncertain)
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=FakeResponse({"ok": True})):
            with self.assertRaises(self.module.LoopDispatchError) as caught:
                self.module._forward_to_loop_sync({"id": 1, "text": "x", "meta": {}}, routing)
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(caught.exception.category, "correlation_missing")
        with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(self.module.LoopDispatchError) as caught:
                self.module._forward_to_loop_sync({"id": 1, "text": "x", "meta": {}}, routing)
            self.assertTrue(caught.exception.uncertain)

    async def test_database_failure_is_retryable_503(self):
        with mock.patch.object(self.module.channel_store, "enqueue_telegram_update",
                               side_effect=sqlite3.OperationalError("closed")):
            response = await request(self.module, "POST", "/integrations/telegram/webhook",
                                     json=update(), headers=webhook_headers())
        self.assertEqual((response.status_code, response.json()["error"]), (503, "temporarily_unavailable"))

    async def test_old_loop_reply_stays_web_compatible_without_telegram_delivery(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        response = await request(self.module, "POST", "/channel/out",
            json={"type": "reply", "text": "legacy", "api_session": "legacy-session"},
            headers={"Authorization": "Bearer test-relay-secret"})
        self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "queued")

    async def test_legacy_ack_marks_correlation_missing_not_failed(self):
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        async def legacy_ack(job, message):
            raise self.module.LoopDispatchError("correlation_missing", True)
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, legacy_ack)
        await worker.run_generation_once()
        with self.module.db() as conn:
            row = conn.execute("SELECT status,error_category FROM generation_jobs").fetchone()
        self.assertEqual((row["status"], row["error_category"]), ("dispatch_uncertain", "correlation_missing"))

    async def test_lifespan_cancels_worker(self):
        started = asyncio.Event(); stopped = asyncio.Event()
        async def forever(worker):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        with mock.patch.object(self.module.TelegramWorker, "run_forever", new=forever):
            async with self.module.lifespan(self.module.app):
                await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(stopped.is_set())

    async def test_production_api_base_restriction(self):
        env = dict(os.environ)
        env.update({"TELEGRAM_ENABLED": "true", "TELEGRAM_TEST_MODE": "false",
                    "TELEGRAM_API_BASE": "http://127.0.0.1:9", "TELEGRAM_API_BASE_ALLOWLIST": ""})
        with mock.patch.dict(os.environ, env, clear=True):
            config = self.module.telegram_integration.TelegramConfig.from_env()
        self.assertFalse(config.enabled); self.assertEqual(config.error, "invalid_config")

    async def test_invalid_timeout_relationship_is_rejected(self):
        with self.assertRaises(ValueError):
            self.module.validate_loop_timeouts(1, 1, 1, 2)

    async def test_network_guard_blocks_every_public_socket_entrypoint(self):
        sock = socket.socket()
        self.addCleanup(sock.close)
        with self.assertRaises(AssertionError): sock.connect(("127.0.0.1", 9))
        with self.assertRaises(AssertionError): sock.connect_ex(("127.0.0.1", 9))
        with self.assertRaises(AssertionError): socket.create_connection(("127.0.0.1", 9))
        with self.assertRaises(AssertionError): socket.getaddrinfo("localhost", 9)


class ConcurrentMigrationTests(unittest.TestCase):
    def test_two_connections_apply_each_version_once_and_preserve_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "db.sqlite3")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,text TEXT)")
                conn.execute("INSERT INTO messages(text) VALUES('keep')")
                conn.execute("CREATE TABLE push_subscriptions(endpoint TEXT PRIMARY KEY)")
                conn.commit()
            async def run():
                await asyncio.gather(asyncio.to_thread(channel_store.run_migrations, path),
                                     asyncio.to_thread(channel_store.run_migrations, path))
            asyncio.run(run())
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0], "keep")
                self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
