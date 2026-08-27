"""Composition root for P2-A shared Codex provider foundations.

No public routes instantiate this object in P2-A. It exists to prove that the P1
account facade and P2 generation facade can share one private App Server runtime
without sharing RPC authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from .codex_account_control_facade import (
    P1_ACCOUNT_NOTIFICATIONS,
    P1_ACCOUNT_RPC_METHODS,
    CodexAccountControlFacade,
)
from .codex_app_server_shared_transport import (
    CodexSharedAppServerRuntime,
    CodexSharedTransportConfig,
)
from .codex_generation_protocol import (
    GENERATION_NOTIFICATIONS,
    GENERATION_RPC_METHODS,
    CodexGenerationConfig,
    CodexGenerationProtocol,
    CodexProcessActivityGate,
    GenerationNotification,
    project_notification,
)


GenerationEventHandler = Callable[[GenerationNotification], Awaitable[None] | None]


class SharedCodexProviderFoundation:
    """One runtime, two scoped facades, zero chat routing."""

    def __init__(
        self,
        transport_config: CodexSharedTransportConfig,
        generation_config: CodexGenerationConfig,
        *,
        generation_event_handler: GenerationEventHandler | None = None,
        _runtime: CodexSharedAppServerRuntime | None = None,
    ) -> None:
        self.activity_gate = CodexProcessActivityGate()
        self.runtime = _runtime or CodexSharedAppServerRuntime(transport_config)
        self._generation_event_handler = generation_event_handler

        control_scope = self.runtime.scope(
            methods=P1_ACCOUNT_RPC_METHODS,
            notifications=P1_ACCOUNT_NOTIFICATIONS,
            handler=self._on_account_notification,
        )
        self.control = CodexAccountControlFacade(control_scope, self.activity_gate)

        generation_scope = self.runtime.scope(
            methods=GENERATION_RPC_METHODS,
            notifications=GENERATION_NOTIFICATIONS,
            handler=self._on_generation_notification,
        )
        self.generation = CodexGenerationProtocol(generation_config, generation_scope)

    async def _on_account_notification(
        self, method: str, params: Mapping[str, object]
    ) -> None:
        await self.control.on_notification(method, params)

    async def _on_generation_notification(
        self, method: str, params: Mapping[str, object]
    ) -> None:
        event = project_notification(method, params)
        if event is None or self._generation_event_handler is None:
            return
        outcome = self._generation_event_handler(event)
        if hasattr(outcome, "__await__"):
            await outcome

    async def close(self) -> None:
        await self.runtime.close()
