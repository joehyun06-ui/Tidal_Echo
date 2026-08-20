from __future__ import annotations

import importlib
import tempfile
import unittest
from unittest import mock

from backend.tests._support import (
    NoNetworkMixin,
    load_app,
    request,
    update,
    webhook_headers,
)


def photo_update(*, update_id: int = 1, message_id: int = 10) -> dict:
    body = update(update_id=update_id, message_id=message_id)
    body["message"].pop("text", None)
    body["message"]["photo"] = [{
        "file_id": f"test-photo-{update_id}",
        "file_size": 1024,
        "width": 640,
        "height": 480,
    }]
    return body


class TelegramImageCompatibilityTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, telegram=True, brain="desktop")
        self.multimodal = importlib.import_module("backend.multimodal_patch")
        self.coalescer = importlib.import_module(
            "backend.telegram_image_followup_coalesce"
        )

    async def test_photo_normalization_preserves_coalescer_image_only_contract(self):
        response = await request(
            self.module,
            "POST",
            "/integrations/telegram/webhook",
            headers=webhook_headers(),
            json=photo_update(),
        )

        self.assertEqual(response.json(), {"ok": True, "queued": True})
        with self.module.db() as conn:
            row = conn.execute("SELECT text,meta FROM messages").fetchone()
        message = {"text": row["text"], "meta": self.module.json.loads(row["meta"])}
        self.assertEqual(
            message["text"],
            self.module.telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER,
        )
        self.assertTrue(self.coalescer._is_image_only(message))

        captioned = dict(message)
        captioned["text"] = "genuine caption"
        self.assertFalse(self.coalescer._is_image_only(captioned))

    async def test_image_followup_coalescing_keeps_canonical_text_persisted(self):
        first = await request(
            self.module,
            "POST",
            "/integrations/telegram/webhook",
            headers=webhook_headers(),
            json=photo_update(),
        )
        second = await request(
            self.module,
            "POST",
            "/integrations/telegram/webhook",
            headers=webhook_headers(),
            json=update(
                update_id=2,
                message_id=11,
                text="question after image",
            ),
        )
        self.assertEqual(first.json(), {"ok": True, "queued": True})
        self.assertEqual(second.json(), {"ok": True, "queued": True})

        with self.module.db() as conn:
            jobs = conn.execute(
                "SELECT * FROM generation_jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(len(jobs), 2)
        with mock.patch.object(
            self.coalescer,
            "_parse_time",
            side_effect=[
                self.coalescer._parse_time(jobs[0]["created_at"]),
                self.coalescer._parse_time(jobs[0]["created_at"]),
            ],
        ):
            followup = self.coalescer._coalesce_followup(
                self.module.DB_PATH,
                dict(jobs[0]),
            )

        self.assertEqual(followup, "question after image")
        with self.module.db() as conn:
            canonical = conn.execute(
                "SELECT text FROM messages ORDER BY id"
            ).fetchall()
            statuses = conn.execute(
                "SELECT status,error_category FROM generation_jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["text"] for row in canonical],
            [
                self.module.telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER,
                "question after image",
            ],
        )
        self.assertEqual(statuses[0]["status"], "queued")
        self.assertEqual(
            (statuses[1]["status"], statuses[1]["error_category"]),
            ("failed", self.coalescer.MERGED_CATEGORY),
        )


if __name__ == "__main__":
    unittest.main()
