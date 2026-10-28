"""Synthetic subprocess crash injector for atomic confirmed memory UOW.

NEVER used by production; takes only test-created SQLite DB and synthetic
epoch/ticket over stdin. Terminates hard with exit 61 at a chosen phase.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from unittest import mock

from backend import memory_action_ledger, memory_explicit_actions
from backend.memory_confirmation_uow_gate import MemoryUowConfirmationGate
from backend.memory_shared_confirmation_ticket import TrustedTicketContext
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests.test_memory_service import bootstrap_runtime, memory_config


def main():
    payload = json.load(sys.stdin)
    if payload["phase"] not in (
        "after_ticket", "after_canonical", "after_ledger", "after_commit"
    ):
        raise RuntimeError("invalid_synthetic_crash_phase")
    if os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("real_api_key_forbidden")

    def deny_socket(*args, **kwargs):
        raise RuntimeError("network_forbidden")

    socket.socket.connect = deny_socket
    socket.create_connection = deny_socket

    for name in (
        "MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED",
        "MEMORY_SESSION_BOUND_WRITE_ENABLED",
        "MEMORY_SESSION_CONFIRMATION_ENABLED",
        "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED",
        "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED",
        "MEMORY_CONFIRMED_UOW_GATE_ENABLED",
    ):
        if os.environ.get(name) != "true":
            raise RuntimeError("synthetic_gate_disabled")

    runtime = bootstrap_runtime(payload["path"], memory_config())
    backend = memory_explicit_actions.create_entry_backend(
        runtime.privileged_actions
    )
    provider = lambda: TrustedTicketContext(
        principal_id="synthetic-user",
        login_id="L" * 32,
        auth_epoch=payload["epoch"],
        session_id="s1",
        canonical_sessions=({"id": "s1"}, {"id": "s2"}),
    )
    gate = MemoryUowConfirmationGate(
        path=payload["path"], context_provider=provider,
    )
    klass = memory_action_ledger._MemoryActionUnitOfWork
    phase = payload["phase"]

    if phase == "after_ticket":
        gate_cls = type(gate)
        original = gate_cls.consume_in_existing_uow

        def terminate_after_ticket(instance, **kwargs):
            result = original(instance, **kwargs)
            os._exit(61)
            return result

        patcher = mock.patch.object(
            gate_cls, "consume_in_existing_uow", new=terminate_after_ticket,
        )
        patcher.start()
    else:
        attr = {
            "after_canonical": "_insert_canonical_action",
            "after_ledger": "complete_request",
            "after_commit": "commit",
        }[phase]
        original = getattr(klass, attr)

        def terminate_after_phase(uow, *args, **kwargs):
            result = original(uow, *args, **kwargs)
            os._exit(61)
            return result

        patcher = mock.patch.object(klass, attr, new=terminate_after_phase)
        patcher.start()

    backend._remember_confirmed_offline_review(
        valid_request(), gate=gate, ticket=payload["ticket"],
        content_sha256=payload["digest"], confirmation_text=payload["phrase"],
    )
    raise RuntimeError("crash_injection_did_not_fire")


if __name__ == "__main__":
    main()
