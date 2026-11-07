# GM-02AE: durable API window-attempt bookkeeping

The default-off WindowAttemptJournal reuses an explicitly supplied enabled
SQLiteLifecycleRepository and its existing canonical messages/lifecycle DB,
config checks and optional cooperative coordinator. It creates no second chat
database. Schema installation is a separate caller-owned migration, never a
startup hook; normal imports/default-off construction perform no IO.

record_attempt binds a request ID to the currently prepared API lifecycle
record's packet digest, revision, closeout ID and access binding. One row per
conversation/closeout and one request ID per conversation are enforced inside
the repository's BEGIN IMMEDIATE transaction. Same-request reconciliation returns
the historical row; a different request for that closeout is rejected, including
after a reentry delta. Changed digest/revision on the same request is a conflict.
Inspection of a missing request returns no row and does not reserve anything.

A returned AttemptObservation ALWAYS reports outcome=unknown,
dispatch_authorized=false, window_confirmed=false and reply_completed=false.
Neither a new row nor a replay is a dispatch permit. This module has no sender,
retry callback, timeout release, confirmation setter or automatic scheduler.
After a precommit rollback the request can be recorded for the first time;
after a lost commit acknowledgement or restart, reconcile the original request.
An observed pre/post config conflict remains an error even if commit may exist.

Tests use disposable canonical SQLite plus private JSON and synthetic lifecycle/
G3 fixtures. They cover lost commit acknowledgement, rollback, config conflict,
two independent repositories racing on one closeout, a fresh subprocess replay,
scope changes, stale revision/digest, delta conflicts, corrupt/oversized rows,
unknown schemas/triggers, closed-phase rejection and explicit Codex refusal.
Canonical messages, JSON, lifecycle state and lifecycle receipts are preserved.

This is preparatory bookkeeping only. It does not fence actual dispatch or
make providers idempotent, integrate API/Codex runtimes, construct a window,
validate current G3 source/ACL revisions, observe source consumption, or prove
chat continuity. The prepared record is checked against the canonical store;
external runtime/evidence freshness must still be checked by a future reviewed
runtime adapter. Read scope is the record's exact access binding, not a new
authorization grant. Checksums detect damage, not adversarial rewrites/rollback.
Trusted code can bypass this journal. Cross-store/uncooperative writer limits of
the repository remain. Existing activation paths do not consult this journal.

No HTTP route, startup/env/default setting, runtime execution, dependency or CI
change is added. Production HOLD and frozen G3 baselines remain unchanged.
