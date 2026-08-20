from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from backend.memory_formation_extractor import (
    EXTRACTOR_CONTRACT_VERSION,
    EXTRACTOR_SESSION_ID,
)
from backend.tests._support import (
    NoNetworkMixin,
    load_app,
    request,
    update,
    webhook_headers,
)


MEMORY_SECRET = "Synthetic-Natural-Ingress-HMAC-Key-2026!R4m8"
RELAY_AUTH = {"Authorization": "Bearer test-relay-secret"}


def extractor_output(proposals: list[dict]) -> str:
    return json.dumps({
        "version": EXTRACTOR_CONTRACT_VERSION,
        "proposals": proposals,
    }, separators=(",", ":"))


def photo_update(
    *,
    update_id: int = 1,
    message_id: int = 10,
    caption: str | None = None,
) -> dict:
    body = update(update_id=update_id, message_id=message_id)
    body["message"].pop("text", None)
    body["message"]["photo"] = [{
        "file_id": f"test-photo-{update_id}",
        "file_size": 1024,
        "width": 640,
        "height": 480,
    }]
    if caption is not None:
        body["message"]["caption"] = caption
    return body


class NaturalIngressFlagOffTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=True,
            brain="desktop",
            kelivo=True,
            memory=True,
            memory_auto_formation=True,
        )

    async def test_flag_off_app_send_creates_no_natural_ingress_task(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
        ) as scheduled:
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={"text": "I prefer tea."},
            )

        self.assertEqual(response.status_code, 200)
        scheduled.assert_not_called()
        self.assertIsNone(getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        ))


class NaturalIngressFormationTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=True,
            brain="desktop",
            kelivo=True,
            memory=True,
            memory_auto_formation=True,
            memory_natural_ingress_formation=True,
        )

    async def asyncTearDown(self):
        task = getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        )
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def wait_for_shadow_idle(self):
        for _ in range(200):
            task = getattr(
                self.module.app.state,
                "memory_formation_shadow_task",
                None,
            )
            if task is None:
                return
            await asyncio.sleep(0.005)
        self.fail("natural-ingress shadow task did not finish")

    async def test_fresh_web_text_schedules_once_without_waiting(self):
        started = asyncio.Event()
        release = asyncio.Event()
        original_scheduler = (
            self.module._schedule_natural_ingress_memory_formation_shadow
        )

        async def runner(**_kwargs):
            started.set()
            await release.wait()

        with mock.patch.object(
            self.module,
            "_run_natural_ingress_memory_formation_shadow_task",
            side_effect=runner,
        ) as run, mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
            wraps=original_scheduler,
        ) as scheduled:
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={"text": "I prefer tea."},
            )
            self.assertEqual(response.status_code, 200)
            await asyncio.wait_for(started.wait(), 2)
            task = self.module.app.state.memory_formation_shadow_task
            self.assertFalse(task.done())
            release.set()
            await task
            await asyncio.sleep(0)

        scheduled.assert_called_once_with(
            canonical_message_id=response.json()["id"],
            channel="web",
            source="relay",
            generation_callable=self.module.KELIVO_GENERATOR,
        )
        run.assert_awaited_once()
        kwargs = run.await_args.kwargs
        self.assertEqual(kwargs["canonical_message_id"], response.json()["id"])
        self.assertEqual((kwargs["channel"], kwargs["source"]), ("web", "relay"))
        self.assertIs(kwargs["generation_callable"], self.module.KELIVO_GENERATOR)

    async def test_web_provenance_is_server_owned_and_client_cannot_override(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
            return_value=True,
        ) as scheduled:
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={
                    "text": "server owns provenance",
                    "channel": "telegram",
                    "source": "client-forgery",
                },
            )

        self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT direction,kind,text,meta FROM messages WHERE id=?",
                (response.json()["id"],),
            ).fetchone()
        meta = self.module.json.loads(row["meta"])
        self.assertEqual((row["direction"], row["kind"]), ("in", "user"))
        self.assertEqual(meta["channel"], "web")
        self.assertEqual(meta["source"], "relay")
        scheduled.assert_called_once_with(
            canonical_message_id=response.json()["id"],
            channel="web",
            source="relay",
            generation_callable=self.module.KELIVO_GENERATOR,
        )

    async def test_attachment_only_web_request_does_not_schedule(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
        ) as scheduled:
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={
                    "text": "   ",
                    "attachments": [{"url": "/relay/uploads/example.png"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        scheduled.assert_not_called()

    async def test_web_scheduler_failure_cannot_change_success(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
            side_effect=RuntimeError("private scheduler failure"),
        ), mock.patch("builtins.print"):
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={"text": "persist this first"},
            )

        self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT text FROM messages WHERE id=?",
                (response.json()["id"],),
            ).fetchone()
        self.assertEqual(row["text"], "persist this first")

    async def test_fresh_telegram_text_schedules_once_and_duplicate_adds_zero(self):
        original_scheduler = (
            self.module._schedule_natural_ingress_memory_formation_shadow
        )
        with mock.patch.object(
            self.module,
            "_run_natural_ingress_memory_formation_shadow_task",
            new_callable=mock.AsyncMock,
        ) as runner, mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
            wraps=original_scheduler,
        ) as scheduled:
            fresh = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=update(),
            )
            await self.wait_for_shadow_idle()
            duplicate = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=update(),
            )
            await asyncio.sleep(0)

        self.assertEqual(fresh.json(), {"ok": True, "queued": True})
        self.assertEqual(duplicate.json(), {"ok": True, "duplicate": True})
        runner.assert_awaited_once()
        kwargs = runner.await_args.kwargs
        self.assertEqual(
            (kwargs["channel"], kwargs["source"]),
            ("telegram", "telegram"),
        )
        self.assertIs(kwargs["generation_callable"], self.module.KELIVO_GENERATOR)
        with self.module.db() as conn:
            row = conn.execute("SELECT id,meta FROM messages").fetchone()
        scheduled.assert_called_once_with(
            canonical_message_id=row["id"],
            channel="telegram",
            source="telegram",
            generation_callable=self.module.KELIVO_GENERATOR,
        )
        self.assertEqual(kwargs["canonical_message_id"], row["id"])
        meta = self.module.json.loads(row["meta"])
        self.assertEqual(meta["channel"], "telegram")
        self.assertEqual(meta["source"], "telegram")

    async def test_ignored_and_rate_limited_telegram_updates_do_not_schedule(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
        ) as scheduled:
            ignored = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=update(chat_type="group"),
            )
            with mock.patch.object(
                self.module.channel_store,
                "enqueue_telegram_update",
                return_value={"duplicate": False, "rejected": "rate_limited"},
            ):
                limited = await request(
                    self.module,
                    "POST",
                    "/integrations/telegram/webhook",
                    headers=webhook_headers(),
                    json=update(update_id=2, message_id=11),
                )

        self.assertTrue(ignored.json()["ignored"])
        self.assertEqual(limited.json()["reason"], "rate_limited")
        scheduled.assert_not_called()

    async def test_telegram_scheduler_failure_preserves_normal_ack(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
            side_effect=RuntimeError("private scheduler failure"),
        ), mock.patch("builtins.print"):
            response = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=update(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "queued": True})
        with self.module.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM messages").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0],
                1,
            )

    async def test_canonical_source_validation_fails_closed(self):
        valid = self.module.save_message(
            "in",
            "user",
            "valid source",
            {"channel": "web", "source": "relay"},
        )
        self.assertEqual(
            self.module._load_natural_ingress_formation_source(
                valid["id"], channel="web", source="relay",
            ),
            (valid["id"], "valid source"),
        )

        wrong_direction = self.module.save_message(
            "out",
            "user",
            "wrong direction",
            {"channel": "web", "source": "relay"},
        )
        wrong_kind = self.module.save_message(
            "in",
            "reply",
            "wrong kind",
            {"channel": "web", "source": "relay"},
        )
        wrong_provenance = self.module.save_message(
            "in",
            "user",
            "wrong provenance",
            {"channel": "web", "source": "client"},
        )
        with self.module.db() as conn:
            cur = conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                (
                    self.module.now_iso(),
                    "in",
                    "user",
                    sqlite3.Binary(b"non-text source"),
                    '{"channel":"web","source":"relay"}',
                ),
            )
            non_text_id = cur.lastrowid
            conn.commit()

        cases = (
            (0, "web", "relay"),
            (wrong_direction["id"], "web", "relay"),
            (wrong_kind["id"], "web", "relay"),
            (non_text_id, "web", "relay"),
            (wrong_provenance["id"], "web", "relay"),
            (valid["id"], "telegram", "relay"),
        )
        for message_id, channel, source in cases:
            with self.subTest(
                message_id=message_id,
                channel=channel,
                source=source,
            ), self.assertRaisesRegex(
                ValueError,
                "^invalid_natural_ingress_source$",
            ):
                self.module._load_natural_ingress_formation_source(
                    message_id,
                    channel=channel,
                    source=source,
                )

        with mock.patch.object(
            self.module.memory_formation_integration,
            "run_memory_formation_shadow",
            new_callable=mock.AsyncMock,
        ) as formation:
            await self.module._run_natural_ingress_memory_formation_shadow_task(
                canonical_message_id=wrong_provenance["id"],
                channel="web",
                source="relay",
                generation_callable=self.module.KELIVO_GENERATOR,
            )
        formation.assert_not_awaited()

    async def test_natural_and_kelivo_formation_share_one_busy_slot(self):
        natural_started = asyncio.Event()
        natural_release = asyncio.Event()

        async def natural_runner(**_kwargs):
            natural_started.set()
            await natural_release.wait()

        with mock.patch.object(
            self.module,
            "_run_natural_ingress_memory_formation_shadow_task",
            side_effect=natural_runner,
        ) as natural_run, mock.patch.object(
            self.module,
            "_run_memory_formation_shadow_task",
            new_callable=mock.AsyncMock,
        ) as kelivo_run, mock.patch("builtins.print"):
            first = self.module._schedule_natural_ingress_memory_formation_shadow(
                canonical_message_id=1,
                channel="web",
                source="relay",
                generation_callable=object(),
            )
            await natural_started.wait()
            second = self.module._schedule_memory_formation_shadow(
                client_id="primary-kelivo",
                idempotency_key="natural-slot-busy-key-0001",
                provider_model="test-provider-model",
                generation_callable=object(),
            )
            self.assertTrue(first)
            self.assertFalse(second)
            natural_run.assert_awaited_once()
            kelivo_run.assert_not_awaited()
            natural_release.set()
            await self.module.app.state.memory_formation_shadow_task
            await asyncio.sleep(0)

        kelivo_started = asyncio.Event()
        kelivo_release = asyncio.Event()

        async def kelivo_runner(**_kwargs):
            kelivo_started.set()
            await kelivo_release.wait()

        with mock.patch.object(
            self.module,
            "_run_memory_formation_shadow_task",
            side_effect=kelivo_runner,
        ) as kelivo_run, mock.patch.object(
            self.module,
            "_run_natural_ingress_memory_formation_shadow_task",
            new_callable=mock.AsyncMock,
        ) as natural_run, mock.patch("builtins.print"):
            first = self.module._schedule_memory_formation_shadow(
                client_id="primary-kelivo",
                idempotency_key="kelivo-slot-busy-key-0001",
                provider_model="test-provider-model",
                generation_callable=object(),
            )
            await kelivo_started.wait()
            second = self.module._schedule_natural_ingress_memory_formation_shadow(
                canonical_message_id=1,
                channel="web",
                source="relay",
                generation_callable=object(),
            )
            self.assertTrue(first)
            self.assertFalse(second)
            kelivo_run.assert_awaited_once()
            natural_run.assert_not_awaited()
            kelivo_release.set()
            await self.module.app.state.memory_formation_shadow_task
            await asyncio.sleep(0)


class NaturalIngressPersistenceTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=True,
            brain="desktop",
            kelivo=True,
            memory=True,
            memory_secret=MEMORY_SECRET,
            memory_auto_formation=True,
            memory_natural_ingress_formation=True,
            memory_candidate_persistence=True,
        )

    async def asyncTearDown(self):
        task = getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        )
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def wait_for_shadow_idle(self):
        for _ in range(200):
            if getattr(
                self.module.app.state,
                "memory_formation_shadow_task",
                None,
            ) is None:
                return
            await asyncio.sleep(0.005)
        self.fail("natural-ingress persistence task did not finish")

    async def test_ineligible_formation_never_calls_candidate_persistence(self):
        source = "Do not remember that I usually prefer coffee."
        selected = "I usually prefer coffee."
        start = source.index(selected)
        calls = []

        async def generate(*args):
            calls.append(args)
            self.assertEqual(args[1], EXTRACTOR_SESSION_ID)
            return {"text": extractor_output([{
                "signal_type": "durable_preference",
                "start": start,
                "end": start + len(selected),
            }])}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted:
            response = await request(
                self.module,
                "POST",
                "/app/send",
                headers=RELAY_AUTH,
                json={"text": source},
            )
            self.assertEqual(response.status_code, 200)
            await self.wait_for_shadow_idle()

        self.assertEqual(len(calls), 1)
        persisted.assert_not_called()


class TelegramImageNaturalIngressExclusionTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=True,
            brain="desktop",
            kelivo=True,
            memory=True,
            memory_secret=MEMORY_SECRET,
            memory_auto_formation=True,
            memory_natural_ingress_formation=True,
            memory_candidate_persistence=True,
        )
        self.multimodal = importlib.import_module("backend.multimodal_patch")

    async def asyncTearDown(self):
        task = getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        )
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def wait_for_shadow_idle(self):
        for _ in range(200):
            if getattr(
                self.module.app.state,
                "memory_formation_shadow_task",
                None,
            ) is None:
                return
            await asyncio.sleep(0.005)
        self.fail("natural-ingress image regression task did not finish")

    async def test_photo_only_persists_and_never_reaches_scheduler_or_slot(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
        ) as scheduled, mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted:
            response = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=photo_update(),
            )

        self.assertEqual(response.json(), {"ok": True, "queued": True})
        scheduled.assert_not_called()
        persisted.assert_not_called()
        self.assertIsNone(getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        ))
        with self.module.db() as conn:
            row = conn.execute("SELECT text,meta FROM messages").fetchone()
            self.assertEqual(
                conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0],
                1,
            )
        meta = json.loads(row["meta"])
        self.assertEqual(
            row["text"],
            self.module.telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER,
        )
        self.assertIsInstance(meta.get("telegram_photo"), dict)

    async def test_photo_only_then_text_leaves_text_formation_opportunity_free(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(**_kwargs):
            started.set()
            await release.wait()

        with mock.patch.object(
            self.module,
            "_run_natural_ingress_memory_formation_shadow_task",
            side_effect=runner,
        ) as run, mock.patch("builtins.print") as printed:
            photo = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=photo_update(),
            )
            self.assertEqual(photo.json(), {"ok": True, "queued": True})
            self.assertIsNone(getattr(
                self.module.app.state,
                "memory_formation_shadow_task",
                None,
            ))

            text = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=update(
                    update_id=2,
                    message_id=11,
                    text="ordinary text follows image",
                ),
            )
            self.assertEqual(text.json(), {"ok": True, "queued": True})
            await asyncio.wait_for(started.wait(), 2)
            task = self.module.app.state.memory_formation_shadow_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())
            release.set()
            await task
            await asyncio.sleep(0)

        run.assert_awaited_once()
        self.assertNotIn(
            "category=busy",
            "\n".join(str(call) for call in printed.call_args_list),
        )

    async def test_direct_photo_only_task_and_scheduler_fail_closed(self):
        representations = (
            {"telegram_photo": {"file_id": "test-photo"}},
            {"attachments": [{
                "url": "/relay/uploads/test-image.png",
                "name": "telegram-image.png",
                "size": 128,
                "mime": "image/png",
                "kind": "image",
            }]},
        )
        for index, media_meta in enumerate(representations, 1):
            with self.subTest(representation=index):
                canonical = self.module.save_message(
                    "in",
                    "user",
                    self.module.telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER,
                    {
                        "channel": "telegram",
                        "source": "telegram",
                        **media_meta,
                    },
                )
                generate = mock.AsyncMock()
                with mock.patch.object(
                    self.module.memory_formation_integration,
                    "run_memory_formation_shadow",
                    new_callable=mock.AsyncMock,
                ) as formation, mock.patch.object(
                    self.module.MEMORY_CANDIDATE_PERSISTENCE,
                    "persist",
                    wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
                ) as persisted, mock.patch("builtins.print") as printed:
                    admitted = (
                        self.module
                        ._schedule_natural_ingress_memory_formation_shadow(
                            canonical_message_id=canonical["id"],
                            channel="telegram",
                            source="telegram",
                            generation_callable=generate,
                        )
                    )
                    await self.module._run_natural_ingress_memory_formation_shadow_task(
                        canonical_message_id=canonical["id"],
                        channel="telegram",
                        source="telegram",
                        generation_callable=generate,
                    )

                self.assertFalse(admitted)
                self.assertIsNone(getattr(
                    self.module.app.state,
                    "memory_formation_shadow_task",
                    None,
                ))
                generate.assert_not_awaited()
                formation.assert_not_awaited()
                persisted.assert_not_called()
                self.assertEqual(
                    [call.args for call in printed.call_args_list],
                    [(
                        "[memory-formation-shadow] "
                        "status=skipped category=source_ineligible",
                    )],
                )

    async def test_telegram_preflight_source_unavailable_logs_once_without_work(
        self,
    ):
        canonical = self.module.save_message(
            "in",
            "user",
            "ordinary Telegram text",
            {"channel": "telegram", "source": "telegram"},
        )
        generate = mock.AsyncMock()
        with mock.patch.object(
            self.module,
            "_load_natural_ingress_formation_source",
            side_effect=RuntimeError("injected canonical reload failure"),
        ), mock.patch.object(
            self.module.memory_formation_integration,
            "run_memory_formation_shadow",
            new_callable=mock.AsyncMock,
        ) as formation, mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted, mock.patch("builtins.print") as printed:
            admitted = (
                self.module._schedule_natural_ingress_memory_formation_shadow(
                    canonical_message_id=canonical["id"],
                    channel="telegram",
                    source="telegram",
                    generation_callable=generate,
                )
            )

        self.assertFalse(admitted)
        self.assertIsNone(getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        ))
        generate.assert_not_awaited()
        formation.assert_not_awaited()
        persisted.assert_not_called()
        self.assertEqual(
            [call.args for call in printed.call_args_list],
            [(
                "[memory-formation-shadow] "
                "status=failed category=source_unavailable",
            )],
        )

    async def test_captioned_photo_formation_receives_caption_only(self):
        caption = "I prefer tea with breakfast."
        seen = []

        async def generate(messages, *_args):
            seen.append(messages[1]["content"])
            return {"text": extractor_output([])}

        self.module.KELIVO_GENERATOR = generate
        response = await request(
            self.module,
            "POST",
            "/integrations/telegram/webhook",
            headers=webhook_headers(),
            json=photo_update(caption=caption),
        )
        self.assertEqual(response.json(), {"ok": True, "queued": True})
        await self.wait_for_shadow_idle()

        self.assertEqual(seen, [caption])
        with self.module.db() as conn:
            row = conn.execute("SELECT text,meta FROM messages").fetchone()
        self.assertEqual(row["text"], caption)
        self.assertIsInstance(json.loads(row["meta"]).get("telegram_photo"), dict)

    async def test_literal_placeholder_text_without_media_remains_eligible(self):
        placeholder = self.module.telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER
        seen = []

        async def generate(messages, *_args):
            seen.append(messages[1]["content"])
            return {"text": extractor_output([])}

        self.module.KELIVO_GENERATOR = generate
        response = await request(
            self.module,
            "POST",
            "/integrations/telegram/webhook",
            headers=webhook_headers(),
            json=update(text=placeholder),
        )
        self.assertEqual(response.json(), {"ok": True, "queued": True})
        await self.wait_for_shadow_idle()
        self.assertEqual(seen, [placeholder])

    async def test_duplicate_photo_update_remains_single_cost(self):
        with mock.patch.object(
            self.module,
            "_schedule_natural_ingress_memory_formation_shadow",
        ) as scheduled:
            first = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=photo_update(),
            )
            duplicate = await request(
                self.module,
                "POST",
                "/integrations/telegram/webhook",
                headers=webhook_headers(),
                json=photo_update(),
            )

        self.assertEqual(first.json(), {"ok": True, "queued": True})
        self.assertEqual(duplicate.json(), {"ok": True, "duplicate": True})
        scheduled.assert_not_called()
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
