import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.tests._support import NoNetworkMixin, load_app, request, update, webhook_headers


class GenerationJobTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)
        await request(self.module, "POST", "/integrations/telegram/webhook", json=update(), headers=webhook_headers())

    async def test_job_claims_once(self):
        first = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        second = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_expired_processing_lease_is_recovered(self):
        first = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.module.db() as conn:
            conn.execute("UPDATE generation_jobs SET lease_until=? WHERE id=?", (expired, first["id"]))
            conn.commit()
        recovered = self.module.channel_store.claim_generation_job(self.module.DB_PATH)
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(recovered["generation_id"], first["generation_id"])

    async def test_worker_uses_injected_dispatcher(self):
        calls = []

        async def dispatcher(job, message):
            calls.append((job["id"], message["id"]))

        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, dispatcher)
        self.assertTrue(await worker.run_generation_once())
        self.assertEqual(len(calls), 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_jobs").fetchone()[0], "awaiting_reply")

    async def test_desktop_target_is_rejected_for_telegram(self):
        self.module.BRAIN_FILE.write_text("desktop", encoding="utf-8")
        worker = self.module.TelegramWorker(self.module.DB_PATH, self.module.TELEGRAM, self.module.dispatch_telegram_generation)
        await worker.run_generation_once()
        with self.module.db() as conn:
            row = conn.execute("SELECT status,error_category FROM generation_jobs").fetchone()
        self.assertEqual((row["status"], row["error_category"]), ("failed", "loop_required"))


if __name__ == "__main__":
    unittest.main()
