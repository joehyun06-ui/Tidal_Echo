# GM-02AI: bind receipt activation to a durable attempt

Default-off activate_recorded_attempt requires the enabled journal and lifecycle
engine to share the same canonical repository. Inside the SAME canonical state/
activation receipt transaction, it validates journal schema/checksum/scope and
the original attempt ID, packet digest, closeout and prepared revision. A delta
after recording invalidates that attempt for activation. The active-state case
permits immediate same-request reconciliation after a lost commit acknowledgement.

The existing activation checks still require a confirmed independently observed
WindowReceipt with matching target window/model run/epoch/source/message cursors
and a current authority snapshot. Missing/unknown/mismatched receipts cannot be
replaced by a journal row or callback return. The activation idempotency input
binds the original attempt ID under a distinct activate_recorded operation, so
legacy activation cannot reuse that receipt ID. State and activation receipt
commit together. The historical journal observation remains unknown.

No receipt is created by this module, no dispatch is performed, no default
activation path is replaced, and no live API/Codex/native adapter is installed.
Tests use synthetic runtime/G3 receipts and real disposable canonical SQLite and
private JSON; even the full packet-handoff-to-activation scenario is synthetic.
A recorded attempt does not prove callback execution or provider consumption.
The runtime must independently authenticate and persist real consumption
receipts; authority must actually fence all relevant writers. These are still
production gates, along with recovery UI, API/Codex separate acceptance, TLS and
backup/recovery. Later state transitions may prevent historical activation
replay; separate historical inspection remains necessary. Default off, no new
schema/startup/HTTP/env/CI/dependency change; production HOLD.
