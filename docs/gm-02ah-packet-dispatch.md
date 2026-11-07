# GM-02AH: hand the validated packet to the trusted adapter once

Default-off attempt_packet_dispatch_once supplies (attempt observation,
ReentryPacket) to a trusted synchronous plain-function adapter. The packet is the
last successfully validated packet rebuilt inside the journal transaction. Its
canonical digest equals the recorded attempt digest. After commit and the final
authority validation it is handed off inside the same authority fence. No
caller-supplied packet/body or independent reconstruction is accepted.

The packet, derived cards and source references use existing frozen dataclasses
and tuple fields. It retains conversation/version, old/target window, epoch,
source/message cursors, pending, reviewed anchors and derived evidence excerpts.
The packet and its cards retain authority=derived; they are not G3 truth.

Historical replay does not release the body again, including after lost commit
ACK or postcommit validation failure. Metadata-only attempt_dispatch_once keeps
its existing adapter contract. Callback exceptions still leave an unknown
attempt, and return values are ignored. No window activation, provider receipt
or source-consumption confirmation is introduced.

This is an explicit trusted in-process data handoff, not a production model
adapter or a permission to transmit source text externally. Tests use synthetic
evidence and adapters only. The authority must actually fence every relevant
writer; frozen dataclasses do not sandbox malicious trusted code. No claim is
made about the adapter honoring target, payload, provider idempotency, retention
or cancellation. No live API/Codex/native adapter, HTTP/startup/env/schema/CI or
dependency change. Actual runtime receipt, independent API/Codex acceptance,
recovery UI, TLS/backup remain open. Default off, production HOLD.
