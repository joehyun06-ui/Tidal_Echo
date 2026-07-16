import tempfile
import unittest

from backend.tests._support import FAKE_USER, NoNetworkMixin, load_app, request, update, webhook_headers


class TelegramWebhookTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)

    async def test_secret_missing_and_wrong(self):
        self.assertEqual((await request(self.module, "POST", "/integrations/telegram/webhook", json=update())).status_code, 401)
        response = await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers("wrong"))
        self.assertEqual(response.status_code, 401)

    async def test_malformed_json(self):
        response = await request(self.module, "POST", "/integrations/telegram/webhook", content=b"{", headers=webhook_headers())
        self.assertEqual(response.status_code, 400)

    async def test_allowlist_and_group_rejected(self):
        denied = await request(self.module, "POST", "/integrations/telegram/webhook",
                               json=update(user_id=FAKE_USER + 99), headers=webhook_headers())
        group = await request(self.module, "POST", "/integrations/telegram/webhook",
                              json=update(chat_type="group"), headers=webhook_headers())
        self.assertEqual((denied.status_code, denied.json()), (200, {"ok": True, "ignored": True, "reason": "not_allowed"}))
        self.assertEqual((group.status_code, group.json()), (200, {"ok": True, "ignored": True, "reason": "not_private"}))

    async def test_empty_overlong_and_command_rejected(self):
        for text in ("   ", "x" * 33, "/admin"):
            response = await request(self.module, "POST", "/integrations/telegram/webhook",
                                     json=update(text=text), headers=webhook_headers())
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ignored"])

    async def test_private_text_is_persisted_and_queued(self):
        response = await request(self.module, "POST", "/integrations/telegram/webhook",
                                 json=update(), headers=webhook_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "queued": True})
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0], 1)
            row = conn.execute("SELECT * FROM messages").fetchone()
            self.assertEqual(row["text"], "hello")


if __name__ == "__main__":
    unittest.main()
