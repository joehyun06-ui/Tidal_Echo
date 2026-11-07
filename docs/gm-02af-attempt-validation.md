# GM-02AF: validated attempt registration, not dispatch

record_validated_attempt is default-off and requires the enabled lifecycle engine
and enabled journal to share the identical canonical repository. It holds the
engine's authority fence across the journal transaction, rebuilds the packet from
current evidence, checks current source/message/pending heads, exact scope,
window/version and safe stream/tool/dispatch boundary, and compares the digest
and revision. Validation runs before journal lookup and after a new insert but
before commit. Revoked/revised evidence or new events/messages require delta or
reject; validated replay cannot promote an old observation into fresh evidence.

A postcommit authority recheck failure returns an explicit uncertainty error;
the original row may exist and must be reconciled by its original request ID.
Historical journal.inspect remains available with its existing exact record
access binding; its unknown result is not current source permission evidence.

The journal's private trusted guard callback adds no authorization. Existing
low-level bookkeeping behavior remains; it may still be called without evidence
validation and never grants dispatch permission. All returned observations remain
unknown / dispatch_authorized=false / window_confirmed=false. No packet body is
returned, runtime receipt consumed, model sent, or existing activation modified.

The caller's authority must really fence all relevant evidence/runtime writers.
Tests use synthetic fenced authority/G3 with actual disposable canonical SQLite
and private JSON; they do not certify production source access or cross-process
exclusion. Point checks alone cannot establish atomicity with nonparticipating
writers. API only, Codex unsupported. No startup/HTTP/env/schema/CI/dependency
changes. Production HOLD and frozen baselines remain.
