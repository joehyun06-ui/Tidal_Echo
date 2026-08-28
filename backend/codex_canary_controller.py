"""P2-B explicit text-only Web canary pin/admission controller.

Normal sessions are untouched. Once a session is explicitly pinned to Codex,
ineligible input fails closed instead of silently crossing back to the API provider.
Admission persists a durable job and returns immediately; generation is worker-owned.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from . import codex_canary_ingress
from . import codex_generation_provider_binding as provider_binding
from . import codex_generation_store as store
from .codex_generation_protocol import CodexGenerationProtocol, input_digest


class CodexCanaryControllerError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexCanaryControllerError category={self.category!r}>"


def _persona_hash(persona: str) -> str:
    return hashlib.sha256(persona.encode("utf-8")).hexdigest()


def _stable_ids(canonical_message_id: int) -> tuple[str, str, str]:
    if isinstance(canonical_message_id, bool) or not isinstance(canonical_message_id, int) or canonical_message_id <= 0:
        raise CodexCanaryControllerError("codex_canary_message_invalid")
    return (
        f"codex-gen-{canonical_message_id}",
        f"codex-client-{canonical_message_id}",
        f"codex-callback-{canonical_message_id}",
    )


class CodexCanaryController:
    def __init__(
        self,
        *,
        store_path: str | Path,
        relay_db: str | Path,
        protocol: CodexGenerationProtocol,
        persona_loader,
    ) -> None:
        self.store_path = Path(store_path)
        self.relay_db = Path(relay_db)
        self.protocol = protocol
        self.persona_loader = persona_loader

    async def pin_session(self, api_session: str) -> Mapping[str, object]:
        persona = self.persona_loader()
        if not isinstance(persona, str) or not persona.strip():
            raise CodexCanaryControllerError("codex_generation_persona_invalid")
        selected = await self.protocol.qualify()
        persona_hash = _persona_hash(persona)
        existing = store.get_session(self.store_path, api_session)
        if existing is not None:
            if (
                existing.get("status") != "active"
                or existing.get("model") != selected.model
                or existing.get("reasoning_effort") != selected.reasoning_effort
                or existing.get("persona_hash") != persona_hash
            ):
                raise CodexCanaryControllerError("codex_canary_session_contract_changed")
            return existing
        try:
            return store.pin_session(
                self.store_path,
                api_session=api_session,
                model=selected.model,
                model_provider=provider_binding.UNRESOLVED_MODEL_PROVIDER,
                reasoning_effort=selected.reasoning_effort,
                persona_hash=persona_hash,
            )
        except store.CodexGenerationStoreError as exc:
            raise CodexCanaryControllerError(exc.category) from None

    def is_pinned(self, api_session: str) -> bool:
        try:
            row = store.get_session(self.store_path, api_session)
        except store.CodexGenerationStoreError:
            return False
        return row is not None and row.get("status") == "active"

    def admit_if_pinned(
        self,
        *,
        canonical_message_id: int,
        api_session: str,
        ingress_text: str,
        continuity_status: str,
    ) -> Mapping[str, object] | None:
        session = store.get_session(self.store_path, api_session)
        if session is None or session.get("status") == "retired":
            return None
        if session.get("status") != "active":
            raise CodexCanaryControllerError("codex_canary_session_unavailable")
        try:
            codex_canary_ingress.require_continuity_empty(continuity_status)
            digest = input_digest(ingress_text)
            canonical_text = codex_canary_ingress.load_text_only_web_message(
                self.relay_db,
                canonical_message_id=canonical_message_id,
                api_session=api_session,
                expected_digest=digest,
            )
            if canonical_text != ingress_text:
                raise CodexCanaryControllerError("codex_canary_input_contract_changed")
            generation_id, client_message_id, callback_identity = _stable_ids(
                canonical_message_id
            )
            job = store.enqueue_job(
                self.store_path,
                api_session=api_session,
                canonical_message_id=canonical_message_id,
                input_digest=digest,
                generation_id=generation_id,
                client_message_id=client_message_id,
                callback_identity=callback_identity,
            )
        except CodexCanaryControllerError:
            raise
        except (codex_canary_ingress.CodexCanaryIngressError, store.CodexGenerationStoreError) as exc:
            raise CodexCanaryControllerError(exc.category) from None
        return {
            "accepted": True,
            "provider": "codex",
            "generation_id": str(job["generation_id"]),
            "client_message_id": str(job["client_message_id"]),
            "callback_identity": str(job["callback_identity"]),
            "api_session": str(job["api_session"]),
            "canonical_message_id": int(job["canonical_message_id"]),
            "status": str(job["status"]),
        }

    def retire_session(self, api_session: str) -> Mapping[str, object]:
        try:
            return store.retire_session(self.store_path, api_session=api_session)
        except store.CodexGenerationStoreError as exc:
            raise CodexCanaryControllerError(exc.category) from None
