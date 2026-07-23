# Memory Core Phase 1

Memory Core is a disabled-by-default, internal-only derived memory layer. It is
not an automatic memory feature and is not connected to any chat or provider
path in Phase 1.

## Data boundaries

The canonical `messages` table is the provenance ledger. It keeps the original
user and assistant messages, stable order, channel/source metadata, and session
correlation. Memory Core never silently edits a canonical message.

`memory_items` contains derived, normalized facts. A memory may be corrected,
superseded, rejected, or forgotten without rewriting its canonical source.
`memory_evidence_events` records a completed, server-authorized action for a
canonical message without copying message text or external identity. Every
event has a unique action ID, a fixed action type, and the action-binding
version; it is an immutable audit record, not a reusable grant.
`memory_sources` links each memory to those events. Database triggers reject
every update or deletion of an evidence event, whether or not it is referenced.
`memory_suppressions` prevents a
forgotten or obsolete fact from being recreated. The singleton
`memory_fingerprint_profile` pins the keyed fingerprint contract.

Provider request context and Heartbeat journal/timeline rows are separate
layers. They are not automatically promoted to user facts.

## Phase 1 scope

Phase 1 exposes an ordinary internal read service for bounded queries. A
separate privileged object provides fixed-purpose operations for:

- explicit create;
- correction through a new revision and supersession link;
- forget through a content-free tombstone and suppression;
- bounded read-only queries and minimal provenance summaries.

It deliberately provides no public Memory HTTP route, model extraction,
automatic canonical-history scan, prompt injection, cross-channel retrieval,
embedding, FTS index, vector database, or full-message cache.

`propose_memory_candidate` and `confirm_memory` remain disabled Phase 2
interfaces. No Phase 1 operation calls a provider.

## Kinds and scopes

Kinds are closed to:

- `user_preference`
- `user_profile`
- `relationship`
- `shared_episode`
- `project`
- `decision`
- `task_or_progress`
- `assistant_experience`

Scope types are `global_user`, `channel`, `session`, and `project`.
`global_user` always uses an empty `scope_ref`; channel scope accepts only the
server's known channel names. Session and project references are internal,
validated opaque identifiers. They are not accepted from an external client in
Phase 1 and must not be logged.

## Runtime authority and service boundary

Application startup calls
`bootstrap_memory_runtime_from_environment(...)` exactly once. That composition
root invokes the formal deployment configuration loader itself and freezes a
`MemoryRuntimePolicy`; `MemoryStore` no longer accepts caller-provided Memory
configuration. Its constructor requires the process-local authority created by
that bootstrap, and repeated bootstrap attempts are rejected without replacing
the current authority. Later environment mutation cannot change the frozen
policy.

The bootstrap also creates an independent random action HMAC secret. It is
generated anew for every process, is separate from the fingerprint secret, and
is never placed in configuration, SQLite, logs, errors, readiness, or object
representations. The application retains only `MemoryReadService`, backed by a
separate read-only query object whose object graph contains neither the
writable Store nor Runtime Authority. It discards `PrivilegedMemoryActions` and
does not place the runtime authority or privileged object in a route,
`app.state`, Telegram, Kelivo, Operit, Galatea, or another adapter.

`MemoryReadService` has no create, correct, forget, or grant method. The
privileged object has only these fixed-semantics actions:

- remember an explicit user message;
- confirm a project decision;
- correct an explicit user memory;
- forget a memory at an explicit user request;
- record an assistant experience.

There is no production testing override, trusted boolean, generic action-type
selector, or caller-configured Store path to writing authority.

## Provenance, action capability, and evidence

Every action requires exactly one new canonical action message and a
server-owned evidence event of one of these types:

- `explicit_user_memory`
- `confirmed_project_decision`
- `explicit_user_correction` (correction only)
- `user_forget` (forget only)
- `assistant_experience` (assistant-only)

After a narrow privileged method receives a trusted explicit action, it creates
a short-lived one-use capability and immediately invokes the Store. The
capability binds a random action ID, fixed action type, canonical message ID,
kind, scope type/reference, normalized content, sensitivity, correction/forget
memory key, normalization/fingerprint domain versions, issue time, expiry, and
binding version using unambiguous canonical JSON and the process-only action
HMAC. The capability is never returned to an ordinary caller and cannot
survive process restart.

`MemoryStore` is the final enforcement point. Inside the final
`BEGIN IMMEDIATE` transaction it verifies the runtime authority and frozen
flags, recomputes the complete business binding, verifies the capability with
constant-time signature comparison, reserves its action ID once, validates the
fingerprint profile, rereads and validates the canonical role/channel/source,
creates the evidence event, applies policy/fingerprint/suppression, writes the
item/source state, and commits. Any failure rolls back the profile, event,
item, source, and suppression and releases the in-process reservation. A
successful action records its unique action ID in the immutable event. A
suppression result rolls back the would-be event/profile and consumes the
one-use capability in memory so it cannot be retried in that process.

Evidence is created only by narrow privileged internal action methods whose
names fix their meaning: explicit user memory, explicit user correction,
explicit user forget, confirmed project decision, or assistant experience.
Each action hard-codes its evidence/reality/subject/component contract and
atomically creates and immediately binds the event to the resulting
memory/source. There is no generic `create_evidence_event(type=...)` interface,
no conversion of an ordinary canonical row into evidence, and no reusable
grant. Missing, forged, changed, expired, cross-purpose, restarted-process, or
already consumed capabilities fail with stable data-free categories and do not
write Memory state.

Ordinary legacy canonical rows have no Memory authorization on their own.
Phase 1 does not wire these privileged actions into Telegram, Kelivo, Operit,
Galatea, an HTTP route, or another chat adapter. A future adapter may receive
only the narrow method corresponding to a reviewed explicit user action. The
system does not infer
whether an ordinary historical message is real, roleplay, a joke, fiction, or
third-party content.

`assistant_experience` follows a separate contract: all evidence must be a
canonical assistant message with a server-owned `assistant_experience` event
created by the assistant runtime and scoped to the assistant. Assistant-only
evidence can never create a user fact.

Provenance queries return only channel, source, evidence role/type, and creation
time. They never return canonical text, canonical IDs, session IDs, Telegram or
device identifiers.

## Normalization, idempotency, and HMAC

Normalization version 1 uses Unicode NFC, normalizes CRLF/CR, trims surrounding
whitespace, and collapses consecutive whitespace. It does not lowercase,
remove punctuation, rewrite facts, or merge synonyms.

The idempotency fingerprint is HMAC-SHA-256 over a versioned,
domain-separated canonical payload containing scope, kind, normalization
version, and normalized content. It uses a dedicated secret and is compared
with constant-time digest comparison. A plain content SHA is never used.

The HMAC key is not stored in SQLite and neither the key, its verifier, nor a
fingerprint is returned, logged, included in exceptions, or exposed by object
`repr`. A separately domain-separated HMAC verifier, stable Key ID,
normalization version, and fingerprint/domain version are stored in the
singleton profile. They are compared fail closed (digest comparison is
constant-time). Phase 1 has no online key/version migration: changing the
Secret, Key ID, normalization version, or domain/fingerprint version requires a
separately reviewed explicit migration. It must never be changed directly.
Concurrent authorized creates and first profile initialization are serialized
by SQLite; a partial unique index also covers live active/candidate
fingerprints.
The profile is not initialized at service construction. Deterministic content
policy runs first, then the first successful action creates the only profile,
evidence event, item, and source in one `BEGIN IMMEDIATE` transaction. Invalid
content or any later provenance/storage failure leaves no profile or grant.
Readiness and every write inspect all profile rows; extra rows, corrupt fields,
a mismatch, or a missing profile alongside any Memory item/source/suppression/
event fail closed.

## Create, correct, and forget

Create writes an active item and its single authorized action source in one
transaction. A later distinct explicit action with an identical live
fingerprint returns the existing public `memory_key` and adds its new audit
source. Reusing the same canonical action is rejected. Sensitivity is
atomically raised to the highest requested classification and never lowered.
Similar text is not automatically merged.

Correct requires an active public `memory_key`. Identical normalized content is
an idempotent no-op. Different content creates a new item, changes the old item
to `superseded`, links it to the new row, and creates a
`corrected_obsolete` suppression in one transaction. The old revision remains
available for audit but is never returned by active retrieval. Same-content
correction atomically adds provenance and applies any sensitivity upgrade.
Correction never lowers an existing sensitivity level.

Forget requires a new explicit user-forget action, changes an active item to
`forgotten`, clears both derived content and the item fingerprint, retains its
non-content audit metadata and provenance, records the `user_forget` evidence,
and creates a `user_forget` suppression atomically. A later separately
authorized forget is an idempotent state operation and adds its own audit
event/source. Phase 1 has no restore or unsuppress operation.

Canonical source messages remain unchanged. Memory forget is therefore not
canonical transcript deletion. SQLite files, WALs, and backups can retain
historical canonical content; a future transcript-erasure workflow requires a
separate storage and backup lifecycle.

The suppression row contains only a keyed HMAC, scope/kind, version, category,
and timestamp. It never stores forgotten text or a reversible copy.

## Privacy policy

The deterministic policy rejects credentials, Authorization/Bearer values,
cookies/session credentials, private keys, common secret formats, financial
credentials, precise coordinate pairs, technical identity values, test/E2E
markers, connection tests, and error-log bodies. Error categories never echo
matched content. Credential scanning checks the preserved normalized content
plus bounded, detection-only views: at most two percent-decoding rounds and a
bounded JSON Unicode-key escape view. These views are never persisted and
never rewrite the normalized memory. Malformed encodings remain inert and do
not produce data-bearing errors.

Sensitive and restricted storage is disabled by default. Sensitive content
cannot be silently downgraded to normal. Existing identical content may still
be reclassified upward while sensitive storage is disabled because that
reduces access; new sensitive/restricted content remains rejected. Phase 1
retrieval always excludes sensitive/restricted items; its internal opt-in
retrieval path remains disabled.
Prompt-injection-looking text is treated only as inert data and cannot change
control flow or select a tool, provider, extractor, scope policy, or SQL.

Canonical metadata is checked before parsing and is limited to 16 KiB UTF-8,
64 total object keys, nesting depth 8, and 4096 characters per key/string. The
top level must be an object. Only bounded `channel` and `source` strings are
used; the full metadata object is never copied into a result or error. Channel
remains required, non-empty, and allowlisted. A missing, JSON-null, or empty
source is normalized to `""`, matching canonical Telegram and Kelivo records;
a non-empty source must pass the existing ASCII/length contract. Non-string and
non-empty whitespace-padded sources fail closed. Operit retains `source=operit`.

## Configuration

```dotenv
MEMORY_CORE_ENABLED=false
MEMORY_EXPLICIT_WRITES_ENABLED=false
MEMORY_SENSITIVE_STORAGE_ENABLED=false
MEMORY_MAX_ITEM_CHARS=1000
MEMORY_FORGET_RETENTION_POLICY=tombstone_without_content
MEMORY_FINGERPRINT_KEY_ID=
MEMORY_FINGERPRINT_HMAC_SECRET=
```

When Memory Core is disabled, no HMAC key is required and `/readyz` reports
`memory_core=false` without making the service unready. Disabled startup applies
and strictly validates core v1–v6; it neither requires nor repairs optional v7.
The shared core validator checks every v1–v6 migration marker, table/column,
primary key, default, CHECK, foreign key, unique/partial index, explicit index,
and trigger set. A recorded migration with a missing or changed core object
returns only `core_schema_invalid` and is never silently repaired by
`CREATE TABLE IF NOT EXISTS`. Optional v7 damage remains isolated only while
Memory is disabled.
Enabled read-only mode atomically applies/validates v7 without requiring a key.
Enabling explicit writes also requires a stable bounded Key ID and a dedicated,
high-entropy 32–512 character printable-ASCII key distinct from all relay,
channel, model, audit, and API-loop credentials. Invalid configuration or
profile mismatch reports only a safe readiness category and rejects all Memory
writes.

## Migration and rollback

Migration v7 only adds `memory_items`, `memory_fingerprint_profile`,
`memory_evidence_events`, `memory_sources`, `memory_suppressions`, and their
indexes and evidence-immutability triggers. It does not rebuild or modify v1-v6
tables or data. Evidence events include unique non-secret `action_id`,
fixed `action_type`, and `action_binding_version=1`; no capability signature or
process secret is persisted. The validator
checks exact columns, CHECK constraints, unique constraints, index
attributes/columns/partial predicates, foreign keys, triggers, and normalized
DDL.

Old v6 application code ignores the additive tables and can continue to read
its existing tables. Current code also treats Memory migration/validation as an
optional atomic path: when Memory is disabled, absent or damaged v7 objects do
not block core v1–v6 startup/readiness. When Memory is enabled, any v7 schema,
configuration, or profile fault fails only Memory readiness closed. Application
rollback is compatible while all Memory features are disabled, but it does not
remove the v7 schema marker or tables. Restore a consistent pre-v7 backup if a
physical schema downgrade is required; never delete only a migration marker or
hand-edit the tables.

## Deferred phases

Phase 2 may add model-assisted candidate extraction with an independently
audited provider call and fail-closed activation policy. Phase 3 may add
bounded, policy-controlled retrieval and prompt context. Neither capability is
implemented or enabled by this change.

## Review status

The third targeted fixes are implemented, but final independent review has not
yet completed. This branch remains Draft and is not ready to merge or deploy;
this document does not claim that every finding is closed.
