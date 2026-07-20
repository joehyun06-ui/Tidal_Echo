# Dylan Heartbeat Foundation

This phase is a local-only persistence and decision foundation. It is disabled
by default and is not attached to the FastAPI lifespan, a scheduler, Telegram,
ntfy, a model provider, or any public endpoint.

## Configuration

The relay validates these values at startup even while heartbeat is disabled:

- `HEARTBEAT_ENABLED=false`
- `HEARTBEAT_INTERVAL_SECONDS=300` (30..86400)
- `HEARTBEAT_TIMEZONE=UTC` (IANA timezone name)
- `HEARTBEAT_QUIET_HOURS_START=22:00` (`HH:MM`)
- `HEARTBEAT_QUIET_HOURS_END=08:00` (`HH:MM`)
- `HEARTBEAT_CONTACT_COOLDOWN_SECONDS=21600` (0..2592000)
- `HEARTBEAT_SCHEDULE_REVISION=default` (1..64 safe ASCII characters)

Equal quiet-hour boundaries are rejected as ambiguous. Quiet hours use the
configured local timezone; every persisted timestamp is normalized to UTC.

## Single-run contract

`backend.heartbeat_service.run_heartbeat_once()` performs one bounded,
network-free transaction. The decision set is restricted to `disabled`,
`quiet_hours`, `cooldown`, `observe`, `journal_candidate`, and
`contact_candidate`. A caller may propose only one of the final three; disabled,
quiet-hour, and cooldown gates take precedence.

Each scheduling configuration has an operator-controlled, stable schedule
revision. A revision is bound on first use to a fingerprint of its interval,
timezone, quiet hours, and cooldown; changing those settings requires a new
revision. A logical tick identity is the schedule revision plus its UTC interval
bucket. `candidate_decision` is deliberately excluded from identity, but it and
all decision configuration are included in the input fingerprint. Reusing an
identity with different inputs returns `input_fingerprint_conflict` instead of
reusing the old decision. A new revision cannot replay the current or an older
completed bucket. Older unseen buckets return `stale_clock`, and state timestamps
never move backward.

The database path is resolved once to an absolute canonical path (including
Windows case normalization), and that exact path is used for the in-process
lock, migration, schema validation, and every SQLite connection. SQLite
`BEGIN IMMEDIATE` provides cross-process serialization. Hard-linked SQLite
database files are unsupported. Network filesystems are outside the supported
deployment boundary because their locking and durability semantics vary.

Replaying a completed logical tick with the same fingerprint returns its stored
result without creating another run, journal entry, or timeline event. A failed
run with the same fingerprint reuses its run row and increments `attempt_count`.
The savepoint execution phase is separate from final commit. A commit error is
reported as `commit_failed` when rollback/non-commit is confirmed and as
`commit_uncertain` when the database cannot be reconciled reliably.

Every completed run writes one short deterministic timeline summary. Journal
rows are created only for `journal_candidate` and `contact_candidate`; their
content is fixed system text and never copies conversation text. Candidate
creation never updates `last_contact_at`. A future delivery phase must update
that field only after externally confirmed contact.

## Migration and backup

Migration v5 adds `heartbeat_state`, `heartbeat_runs`, `journal_entries`, and
`timeline_events` plus their bounded lookup indexes. Startup validates exact
columns, constraints, indexes, foreign keys, and DDL fingerprints. Backups made
after v5 should include all four tables together with `schema_migrations`; v1-v4
Telegram and Kelivo tables are not rebuilt or changed by this migration.

Migration v6 adds `heartbeat_schedule_revisions` and `heartbeat_run_inputs`.
These tables bind safe scheduling fingerprints and input fingerprints to v5 run
rows without rebuilding or rewriting existing Telegram, Kelivo, or heartbeat
data.

## Explicitly not implemented

There is no scheduler, infinite loop, model-generated journal content, message
composition, Telegram/ntfy delivery, UI, SSE, Galatea, Cedar Toy, Operit,
Render configuration change, deployment, or public API in this phase.
