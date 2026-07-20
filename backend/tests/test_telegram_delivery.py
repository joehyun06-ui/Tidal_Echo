import io
import json
import logging
import tempfile
import unittest
import urllib.error

from backend.tests._support import FAKE_TOKEN, NoNetworkMixin, load_app, request, update, webhook_headers


class FakeResponse:
    def __init__(self, message_id=9001):
        self.payload = json.dumps({"ok": True, "result": {"message_id": message_id}}).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size=-1): return self.payload if size < 0 else self.payload[:size]


class TelegramDeliveryTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        self.job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        self.job = self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, self.job["id"])
        self.module.channel_store.finish_generation_dispatch(self.module.DB_PATH, self.job["id"], "awaiting_reply")

    def complete(self, text="answer"):
        meta = {key: self.job[key] for key in ("stream_id", "generation_id", "reply_to", "api_session")}
        meta.update({"channel": "telegram", "channel_account": self.job["external_account_id"],
                     "channel_conversation": self.job["external_conversation_id"]})
        return self.module.telegram_completion_for({"kind": "reply", "text": text, "meta": meta})["message"]

    async def test_uncorrelated_reply_never_creates_delivery(self):
        self.module.telegram_completion_for({"kind": "reply", "text": "no route", "meta": {"generation_id": "unknown", "reply_to": "1", "api_session": "x"}})
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT error_category FROM channel_audit_events WHERE event_type='reply_uncorrelated'"
            ).fetchone()[0], "correlation_missing")

    async def test_final_reply_is_idempotent(self):
        msg = self.complete()
        self.module.telegram_completion_for(msg)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 1)

    async def test_success_saves_external_message_id(self):
        self.complete()
        client = self.module.telegram_integration.TelegramClient(self.module.TELEGRAM, opener=lambda *a, **k: FakeResponse())
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, lambda *_: None, client)
        await worker.run_delivery_once()
        with self.module.db() as conn:
            delivery = conn.execute("SELECT status,external_message_id FROM delivery_attempts").fetchone()
            mapping = conn.execute("SELECT external_message_id FROM external_messages WHERE direction='out'").fetchone()
        self.assertEqual((delivery["status"], delivery["external_message_id"]), ("delivered", "9001"))
        self.assertEqual(mapping["external_message_id"], "9001")

    async def test_definite_failure_is_not_retried(self):
        self.complete()
        def fail(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO())
        client = self.module.telegram_integration.TelegramClient(self.module.TELEGRAM, opener=fail)
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, lambda *_: None, client)
        await worker.run_delivery_once()
        self.assertFalse(await worker.run_delivery_once())
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM delivery_attempts").fetchone()[0], "failed")

    async def test_timeout_is_uncertain_and_token_not_exposed(self):
        self.complete()
        def timeout(*args, **kwargs): raise TimeoutError("transport timed out")
        client = self.module.telegram_integration.TelegramClient(self.module.TELEGRAM, opener=timeout)
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, lambda *_: None, client)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            await worker.run_delivery_once()
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertNotIn(FAKE_TOKEN, stream.getvalue())
        self.assertFalse(await worker.run_delivery_once())
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM delivery_attempts").fetchone()[0], "delivery_uncertain")

    async def test_restart_marks_inflight_delivery_uncertain(self):
        self.complete()
        claimed = self.module.channel_store.claim_delivery(self.module.DB_PATH)
        self.assertIsNotNone(claimed)
        self.assertEqual(self.module.channel_store.recover_inflight_deliveries(self.module.DB_PATH), 1)
        self.assertIsNone(self.module.channel_store.claim_delivery(self.module.DB_PATH))
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM delivery_attempts").fetchone()[0], "delivery_uncertain")

    async def test_plain_text_chunking_is_lossless(self):
        text = "abc\ndefghijkl"
        chunks = self.module.telegram_integration.split_plain_text(text, 5)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 5 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
