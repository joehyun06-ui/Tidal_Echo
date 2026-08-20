"""Coalesce an image-only Telegram update with an immediate text follow-up.

Telegram sends a photo and a separately-sent question as two updates. Without a
small grace window the worker generates once for the image, then again for the
text. This compatibility patch delays only image-only generation briefly and,
when the same private conversation queues a plain-text follow-up in that window,
uses that text as the image prompt while suppressing the redundant second
model dispatch.

The canonical text update remains stored as the user's real message. No image
bytes, message text, IDs, tokens, paths, or URLs are logged by this module.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from backend import channel_store, telegram_integration


COALESCE_SECONDS = 3.0
MAX_FOLLOWUP_AGE_SECONDS = 5.0
MERGED_CATEGORY = "coalesced_image_followup"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_image_only(message: dict) -> bool:
    try:
        meta = message.get("meta") or {}
        return (
            isinstance(meta, dict)
            and isinstance(meta.get("telegram_photo"), dict)
            and str(message.get("text") or "").strip()
            == telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER
        )
    except Exception:
        return False


def _coalesce_followup(db_path: str, current_job: dict) -> str:
    """Atomically suppress one queued plain-text follow-up and return its text."""
    current_created = _parse_time(current_job.get("created_at"))
    if current_created is None:
        return ""

    stamp = datetime.now(timezone.utc).isoformat()
    with channel_store.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """SELECT gj.id AS job_id, gj.created_at AS job_created_at,
                          m.text AS text, m.meta AS meta
                   FROM generation_jobs AS gj
                   JOIN messages AS m ON m.id=gj.canonical_message_id
                   WHERE gj.channel='telegram'
                     AND gj.external_account_id=?
                     AND gj.external_conversation_id=?
                     AND gj.status='queued'
                     AND gj.id>?
                   ORDER BY gj.id ASC
                   LIMIT 3""",
                (
                    current_job["external_account_id"],
                    current_job["external_conversation_id"],
                    current_job["id"],
                ),
            ).fetchall()

            for row in rows:
                follow_created = _parse_time(row["job_created_at"])
                if follow_created is None:
                    continue
                age = (follow_created - current_created).total_seconds()
                if age < 0 or age > MAX_FOLLOWUP_AGE_SECONDS:
                    continue
                text = str(row["text"] or "").strip()
                if not text or text == telegram_integration.TELEGRAM_IMAGE_PLACEHOLDER:
                    continue
                try:
                    meta = json.loads(row["meta"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(meta, dict) and meta.get("telegram_photo"):
                    continue

                updated = conn.execute(
                    """UPDATE generation_jobs
                       SET status='failed', lease_until=NULL, error_category=?, updated_at=?
                       WHERE id=? AND status='queued'""",
                    (MERGED_CATEGORY, stamp, row["job_id"]),
                )
                if updated.rowcount == 1:
                    conn.execute("COMMIT")
                    return text

            conn.execute("COMMIT")
            return ""
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


async def _run_generation_once(self: telegram_integration.TelegramWorker) -> bool:
    job = channel_store.claim_generation_job(
        self.db_path,
        max_attempts=self.config.generation_max_attempts,
    )
    if not job:
        return False
    if not self.config.enabled:
        channel_store.start_generation_dispatch(self.db_path, job["id"])
        channel_store.finish_generation_dispatch(
            self.db_path,
            job["id"],
            "failed",
            "telegram_disabled",
        )
        return True

    message = self._canonical_message(job["canonical_message_id"])
    if not message:
        channel_store.start_generation_dispatch(self.db_path, job["id"])
        channel_store.finish_generation_dispatch(
            self.db_path,
            job["id"],
            "failed",
            "canonical_message_missing",
        )
        return True

    if _is_image_only(message):
        await asyncio.sleep(COALESCE_SECONDS)
        try:
            followup = await asyncio.to_thread(
                _coalesce_followup,
                self.db_path,
                job,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            followup = ""
        if followup:
            message = dict(message)
            message["text"] = followup
            print("[telegram-image-turn] merged=true", flush=True)

    job = channel_store.start_generation_dispatch(self.db_path, job["id"])
    if not job:
        return True
    try:
        await self.generation_dispatcher(job, message)
    except asyncio.CancelledError:
        raise
    except telegram_integration.LoopDispatchError as exc:
        outcome = "dispatch_uncertain" if exc.uncertain else "failed"
        channel_store.finish_generation_dispatch(
            self.db_path,
            job["id"],
            outcome,
            exc.category,
        )
    except Exception:
        channel_store.finish_generation_dispatch(
            self.db_path,
            job["id"],
            "dispatch_uncertain",
            "unexpected_dispatch_error",
        )
    else:
        channel_store.finish_generation_dispatch(
            self.db_path,
            job["id"],
            "awaiting_reply",
        )
    return True


def install() -> None:
    telegram_integration.TelegramWorker.run_generation_once = _run_generation_once
