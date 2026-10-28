"""Read-only verification of a test-only persisted audit from new process."""
from __future__ import annotations

import json
import os
import socket
import sys

from backend.memory_durable_consent_audit import DurableObservedConsentAudit
from backend.tests._memory_session_confirm_worker import valid_request


def main() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("network_test_with_live_key_disallowed")
    socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("network_disallowed")
    )
    payload = json.load(sys.stdin)
    audit = DurableObservedConsentAudit(
        path=payload["db"],
        context_provider=lambda: None,
        interaction_provider=lambda: None,
        hmac_secret=bytes.fromhex(payload["secret_hex"]),
    )
    record = audit.verify_historical(
        valid_request(), nonce=payload["nonce"],
    )
    print("verified:" + record.action)


if __name__ == "__main__":
    main()
