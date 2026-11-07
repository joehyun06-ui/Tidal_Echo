# GM-02AJ: historical original activation status without retry

Default-off inspect_recorded_activation reads the original attempt and activation
receipt in one query-only canonical observation. It never dispatches, queries a
runtime, activates, creates an intent, recovers a job or grants retry. The
repository observation uses PRAGMA query_only=ON and BEGIN (not BEGIN IMMEDIATE),
retaining canonical schema/checksum, session/version and config checks. Both
SQL writes and transaction.save are rejected on this observation.

States: attempt_missing means no matching attempt or receipt was observed;
unconfirmed means the attempt exists but no activation receipt was observed;
historical_confirmed requires the exact activate_recorded input digest, original
attempt ID/packet/scope and applied revision. Missing or unconfirmed is never
proof that external dispatch did not happen. All states keep retry_authorized,
current_window_confirmed and reply_completed false. Only historical_confirmed
sets window_confirmed_at_apply true.

Later closeout/state transitions do not erase a verified historical receipt.
Reading it is not current G3 source permission verification, current window
availability or a fresh provider acknowledgement. Current exact record access
binding, canonical version ownership and config validity remain required; stale
or changed access is denied. Legacy activation receipts cannot be relabeled as
recorded-attempt confirmations. Checksums are not signatures or rollback proof.

Synthetic tests include lost activation ACK, new process reads, later closeout,
read-only enforcement and repeated reads preserving all rows/config. No live
runtime/API/Codex adapter, public HTTP route, UI, schema/startup/env/dependency or
CI change is installed. Production runtime/source-consumption and recovery UI,
separate API/Codex acceptance, TLS/backup remain open. Production HOLD.
