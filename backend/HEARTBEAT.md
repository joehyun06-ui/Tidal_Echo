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

Equal quiet-hour boundaries are rejected as ambiguous. Quiet hours use the
configured local timezone; every persisted timestamp is normalized to UTC.

## Single-run contract

`backend.heartbeat_service.run_heartbeat_once()` performs one bounded,
network-free transaction. The decision set is restricted to `disabled`,
`quiet_hours`, `cooldown`, `observe`, `journal_candidate`, and
`contact_candidate`. A caller may propose only one of the final three; disabled,
quiet-hour, and cooldown gates take precedence.

Ticks are bucketed by the configured interval and deduplicated by a deterministic
SHA-256 identity. A per-database process lock prevents thread re-entry and
SQLite `BEGIN IMMEDIATE` provides cross-process serialization. Replaying a
completed bucket returns its stored result without creating another run,
journal entry, or timeline event. A failed or interrupted run is safe to retry
because this phase performs no external side effect.

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

## Explicitly not implemented

There is no scheduler, infinite loop, model-generated journal content, message
composition, Telegram/ntfy delivery, UI, SSE, Galatea, Cedar Toy, Operit,
Render configuration change, deployment, or public API in this phase.
