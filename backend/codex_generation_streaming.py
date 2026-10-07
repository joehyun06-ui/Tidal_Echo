"""P3 streaming and exact-turn interruption over the existing durable worker."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import closing

from pathlib import Path

from . import codex_generation_store as store
from .codex_canary_ingress import CodexCanaryIngressError
from .codex_generation_images import load_image_web_message
from .codex_generation_protocol import CodexGenerationError
from .codex_generation_progress import PROGRESS_NOTIFICATIONS, ReplyProgress, safe_id
from .codex_generation_subscription_reliability import ResubscribingCodexGenerationRuntime


class GenerationControlError(RuntimeError):
    def __init__(self, category, status_code=409):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


def find_job(path, api_session, generation_id=None):
    if not safe_id(api_session) or (generation_id is not None and not safe_id(generation_id)):
        raise GenerationControlError("generation_request_invalid", 400)
    with closing(store.connect(path)) as conn:
        row = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE api_session=?"
            + (" AND generation_id=?" if generation_id else "")
            + " ORDER BY id DESC LIMIT 1",
            (api_session, generation_id) if generation_id else (api_session,),
        ).fetchone()
    if row is None and generation_id:
        raise GenerationControlError("generation_not_found", 404)
    return dict(row) if row else None


class GenerationControls:
    def __init__(self, path, protocol, progress):
        self.path, self.protocol, self.progress = path, protocol, progress
        self.stops = OrderedDict()

    def status(self, api_session, generation_id=None):
        job = find_job(self.path, api_session, generation_id)
        if job is None:
            return {"ok": True, "contract_version": 1, "api_session": api_session, "generation": None}
        status = job["status"]
        terminal = status in {"completed", "failed"}
        if terminal:
            self.stops.pop(job["generation_id"], None)
            status = "interrupted" if job.get("error_category") == "codex_turn_interrupted" else status
        elif status == "in_progress":
            status = self.stops.get(job["generation_id"], "running")
        elif status in {"processing", "thread_dispatching", "turn_dispatching"}:
            status = "starting"
        generation = {
            "id": job["generation_id"], "status": status, "terminal": terminal,
            "canonical_message_id": job["canonical_message_id"],
            "assistant_message_id": job.get("assistant_message_id"),
            "can_stop": not terminal and job["generation_id"] not in self.stops
                and (job["status"] == "queued" or (job["status"] == "in_progress" and bool(job.get("turn_id")))),
            "updated_at": job["updated_at"],
        }
        return {
            "ok": True, "contract_version": 1, "api_session": api_session,
            "generation": generation,
            "snapshot": self.progress.snapshot(job) if not terminal else None,
        }

    async def stop(self, api_session, generation_id):
        job = find_job(self.path, api_session, generation_id)
        if job["status"] in {"completed", "failed"}:
            return self.status(api_session, generation_id)
        # CAS: a queued job may be claimed between the read and this transaction.
        if job["status"] == "queued":
            with closing(store.connect(self.path)) as conn:
                conn.execute(
                    "UPDATE codex_generation_jobs SET status='failed',error_category='codex_turn_interrupted',"
                    "lease_until=NULL,updated_at=? WHERE id=? AND status='queued'",
                    (store.now_iso(), job["id"]),
                )
            job = find_job(self.path, api_session, generation_id)
            if job["status"] == "failed":
                return self.status(api_session, generation_id)
        if generation_id in self.stops:
            return self.status(api_session, generation_id)
        if job["status"] != "in_progress" or not job.get("thread_id") or not job.get("turn_id"):
            raise GenerationControlError("generation_not_interruptible")
        self.stops[generation_id] = "stopping"
        while len(self.stops) > 32:
            self.stops.popitem(last=False)
        try:
            # The generation facade owns this exact turn. P1's control gate must
            # not reject it just because that same generation is currently active.
            await self.protocol.interrupt(thread_id=job["thread_id"], turn_id=job["turn_id"])
        except Exception:
            self.stops[generation_id] = "stop_uncertain"
        # An empty RPC result only acknowledges the request; terminal status is
        # read from the worker receipt after turn/completed (including races).
        return self.status(api_session, generation_id)


class StreamingCodexGenerationRuntime(ResubscribingCodexGenerationRuntime):
    def __init__(self, *, progress_callback, upload_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.upload_dir = Path(upload_dir) if upload_dir is not None else None
        self.controller.upload_dir = self.upload_dir
        self.progress = ReplyProgress()
        self.controls = GenerationControls(self.config.store_path, self.foundation.generation, self.progress)
        self.progress_callback = progress_callback
        self._progress_task = None
        self._progress_scope = self.foundation.runtime.scope(
            methods=frozenset(), notifications=PROGRESS_NOTIFICATIONS,
            handler=self._on_progress,
        )

    def _on_progress(self, method, params):
        self.progress.receive(method, params)

    def _load_canonical_message(self, job):
        if self.upload_dir is None:
            return super()._load_canonical_message(job)
        try:
            return load_image_web_message(
                self.relay_db, canonical_message_id=int(job["canonical_message_id"]),
                api_session=str(job["api_session"]), upload_dir=self.upload_dir,
                expected_digest=str(job["input_digest"]),
            )
        except CodexCanaryIngressError as error:
            raise CodexGenerationError(error.category) from None

    async def start(self):
        await super().start()
        if self.generation_enabled and (self._progress_task is None or self._progress_task.done()):
            self._progress_task = asyncio.create_task(self._publish_progress(), name="codex-reply-progress")

    async def _publish_progress(self):
        sent = {}
        while True:
            self.progress.prune()
            # Coalescing full snapshots bounds HTTP traffic and never drops text.
            for (thread, turn), record in tuple(self.progress.turns.items()):
                key = (thread, turn)
                if sent.get(key) == record["revision"]:
                    continue
                try:
                    with closing(store.connect(self.config.store_path)) as conn:
                        row = conn.execute(
                            "SELECT * FROM codex_generation_jobs WHERE thread_id=? AND turn_id=? "
                            "AND status IN ('in_progress','dispatch_uncertain') ORDER BY id DESC LIMIT 1",
                            key,
                        ).fetchone()
                    if row is not None:
                        snapshot = self.progress.snapshot(dict(row))
                        await self.progress_callback(snapshot)
                        sent[key] = snapshot["revision"]
                except Exception:
                    # Progress is ephemeral. A failed publish must not fail the
                    # durable final reply; authenticated GET can repair a gap.
                    pass
            sent = {key: value for key, value in sent.items() if key in self.progress.turns}
            await asyncio.sleep(0.15)

    async def close(self):
        if self._progress_task:
            self._progress_task.cancel()
            try:
                await self._progress_task
            except asyncio.CancelledError:
                pass
            self._progress_task = None
        self.progress.turns.clear()
        await super().close()


def build_progress_callback(legacy):
    async def callback(snapshot):
        ok, _body, _uncertain = await legacy.relay_out({
            **snapshot, "type": "reply_delta", "snapshot": True,
        })
        if not ok:
            raise GenerationControlError("generation_progress_unavailable", 503)
    return callback
