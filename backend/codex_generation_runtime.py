"""P2-B default-off composition root for api-loop integration.

The runtime owns one shared Codex foundation, a durable generation store, one worker
loop, and the explicit canary controller. It does not register HTTP routes itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import codex_canary_ingress
from . import codex_generation_store as store
from .codex_canary_controller import CodexCanaryController
from .codex_generation_runtime_config import (
    CodexGenerationRuntimeConfig,
    compose_shared_transport_config,
    prepare_generation_paths,
)
from .codex_generation_worker import CodexGenerationEventInbox, CodexGenerationWorker
from .codex_shared_provider_foundation import SharedCodexProviderFoundation
from .deployment_config import CodexControlConfig


class CodexGenerationRuntime:
    def __init__(
        self,
        *,
        control_config: CodexControlConfig,
        generation_config: CodexGenerationRuntimeConfig,
        relay_db: str | Path,
        persona_loader,
        completion_callback,
        _foundation: SharedCodexProviderFoundation | None = None,
    ) -> None:
        self.control_config = control_config
        self.config = generation_config
        self.relay_db = Path(relay_db)
        self.persona_loader = persona_loader
        self.event_inbox = CodexGenerationEventInbox()
        self.foundation = _foundation or SharedCodexProviderFoundation(
            compose_shared_transport_config(control_config, generation_config),
            generation_config.generation,
            control_enabled=control_config.enabled,
            generation_event_handler=self.event_inbox.on_event,
        )
        self.controller = CodexCanaryController(
            store_path=generation_config.store_path,
            relay_db=self.relay_db,
            protocol=self.foundation.generation,
            persona_loader=persona_loader,
        )
        self.worker = CodexGenerationWorker(
            store_path=generation_config.store_path,
            protocol=self.foundation.generation,
            activity_gate=self.foundation.activity_gate,
            persona_loader=persona_loader,
            canonical_message_loader=self._load_canonical_message,
            completion_callback=completion_callback,
            event_inbox=self.event_inbox,
        )
        self._worker_task: asyncio.Task | None = None

    @property
    def control(self):
        return self.foundation.control

    @property
    def generation_enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        prepare_generation_paths(self.config, self.control_config)
        store.initialize(self.config.store_path)
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="codex-generation-worker"
        )

    async def _worker_loop(self) -> None:
        while True:
            worked = await self.worker.run_once()
            if not worked:
                await asyncio.sleep(self.config.poll_interval_seconds)

    def _load_canonical_message(self, job) -> str:
        return codex_canary_ingress.load_text_only_web_message(
            self.relay_db,
            canonical_message_id=int(job["canonical_message_id"]),
            api_session=str(job["api_session"]),
            expected_digest=str(job["input_digest"]),
        )

    def worker_healthy(self) -> bool:
        if not self.config.enabled:
            return True
        return self._worker_task is not None and not self._worker_task.done()

    def worker_exception(self) -> BaseException | None:
        task = self._worker_task
        if task is None or not task.done() or task.cancelled():
            return None
        try:
            return task.exception()
        except asyncio.CancelledError:
            return None

    async def close(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Worker health is exposed separately through worker_exception().
                # Shutdown must still close the one shared App Server runtime.
                pass
        await self.foundation.close()
