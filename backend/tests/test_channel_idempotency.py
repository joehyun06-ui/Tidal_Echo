import json
import tempfile
import unittest

from backend.tests._support import FAKE_CHAT, NoNetworkMixin, load_app, request, update, webhook_headers


class ChannelIdempotencyTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)

    async def post(self, body):
        return await request(self.module, "POST", "/integrations/telegram/webhook", json=body, headers=webhook_headers())

    async def test_duplicate_update_creates_no_duplicate_cost_or_message(self):
        self.assertEqual((await self.post(update())).status_code, 200)
        duplicate = await self.post(update())
        self.assertEqual(duplicate.json(), {"ok": True, "duplicate": True})
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM external_messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    async def test_same_chat_reuses_random_session_and_different_chat_isolated(self):
        await self.post(update(update_id=1, message_id=10))
        await self.post(update(update_id=2, message_id=11))
        await self.post(update(update_id=3, message_id=12, chat_id=FAKE_CHAT + 1))
        with self.module.db() as conn:
            rows = conn.execute("SELECT external_conversation_id,api_session FROM channel_conversations ORDER BY id").fetchall()
            metas = [json.loads(row["meta"]) for row in conn.execute("SELECT meta FROM messages ORDER BY id")]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["api_session"], str(FAKE_CHAT))
        self.assertEqual(metas[0]["api_session"], metas[1]["api_session"])
        self.assertNotEqual(metas[1]["api_session"], metas[2]["api_session"])


if __name__ == "__main__":
    unittest.main()
