import asyncio
import tempfile
import unittest

from backend.tests._support import NoNetworkMixin, load_app, request, update, webhook_headers


class ExistingRelayBaselineTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, telegram=True, brain="desktop")

    async def test_required_env_creates_legacy_tables_and_sse_shape(self):
        with self.module.db() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"messages", "push_subscriptions"}.issubset(tables))
        self.assertEqual(self.module.SSE_HEADERS["X-Accel-Buffering"], "no")
        self.assertTrue(self.module.sse_data({"ok": True}).startswith("data: "))

    async def test_app_send_auth_save_and_history_session_isolation(self):
        denied = await request(self.module, "POST", "/app/send", json={"text": "one"})
        self.assertEqual(denied.status_code, 401)
        auth = {"Authorization": "Bearer test-relay-secret"}
        for session, text in (("session-a", "one"), ("session-b", "two")):
            response = await request(self.module, "POST", "/app/send", json={"text": text, "api_session": session}, headers=auth)
            self.assertEqual(response.status_code, 200)
        history = await request(self.module, "GET", "/app/history?session_id=session-a", headers=auth)
        self.assertEqual([m["text"] for m in history.json()["messages"]], ["one"])

    async def test_telegram_message_and_final_reply_share_web_history_path(self):
        self.module.BRAIN_FILE.write_text("loop", encoding="utf-8")
        incoming = await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())
        self.assertEqual(incoming.status_code, 200)
        job = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        job = self.module.channel_store.start_generation_dispatch(self.module.DB_PATH, job["id"])
        self.module.channel_store.finish_generation_dispatch(self.module.DB_PATH, job["id"], "awaiting_reply")
        payload = {
            "type": "reply", "text": "final answer", "stream_id": job["stream_id"],
            "generation_id": job["generation_id"], "reply_to": job["reply_to"], "api_session": job["api_session"],
            "channel": "telegram", "channel_account": job["external_account_id"],
            "channel_conversation": job["external_conversation_id"],
        }
        auth = {"Authorization": "Bearer test-relay-secret"}
        queue = asyncio.Queue()
        self.module.app_subs.add(queue)
        try:
            reply = await request(self.module, "POST", "/channel/out", json=payload, headers=auth)
            self.assertEqual(reply.status_code, 200)
            self.assertEqual((await queue.get())["type"], "typing")
            self.assertEqual((await queue.get())["text"], "final answer")
        finally:
            self.module.app_subs.discard(queue)
        history = await request(self.module, "GET", f"/app/history?session_id={job['api_session']}", headers=auth)
        messages = history.json()["messages"]
        self.assertEqual([(m["from"], m["text"]) for m in messages], [("human", "hello"), ("ai", "final answer")])
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
