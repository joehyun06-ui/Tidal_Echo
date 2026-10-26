"""Synthetic-only subprocess to prove shared-DB ticket consumption."""
import json
import sys
from pathlib import Path
from backend.memory_shared_confirmation_ticket import (
    SharedConfirmationTickets, TrustedTicketContext,
)
from backend.tests._memory_session_confirm_worker import valid_request

def main():
    if len(sys.argv) != 2:
        return 2
    payload = json.loads(sys.stdin.read())
    store = SharedConfirmationTickets(
        path=Path(sys.argv[1]),
        context_provider=lambda: TrustedTicketContext(
            payload["principal"], payload["login"], payload["epoch"],
            payload["session"], ({"id": "s1"}, {"id": "s2"}),
        ),
    )
    store.claim(
        ticket=payload["ticket"], content_sha256=payload["digest"],
        confirmation_text="CONFIRM MEMORY", request=valid_request(),
    )
    print("CLAIMED_OFFLINE")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
