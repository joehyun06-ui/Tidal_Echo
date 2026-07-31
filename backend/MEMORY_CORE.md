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

## Threat model

Phase 1 trusts the reviewed repository code that runs in the same Python
interpreter as the backend application, including the application composition
root and internal modules admitted to production through code review. Code that
can import production modules and execute arbitrary Python in that interpreter
is therefore inside the trusted computing base.

Phase 1 treats all of the following as untrusted:

- HTTP, Telegram, Kelivo, Operit, and Galatea client input;
- canonical message text and metadata, and Memory item text;
- external URLs, tool instructions, and prompt-injection content;
- database state that may be corrupt or forged;
- replays, concurrency, timeouts, and uncertain network outcomes.

Python module-private variables, underscore-prefixed names, closures, and object
identity are not security isolation boundaries against an arbitrary
same-interpreter code executor. Runtime Policy, Privileged Actions, and Action
Capabilities instead provide a clear application composition boundary, keep
ordinary paths from accidentally acquiring writes, bind authorization to one
specific action, reject external forgery/tampering/expiry/replay, and preserve
one-use and database-transaction atomicity. They do not claim to sandbox or
contain a malicious module that already has arbitrary code execution in the
backend interpreter.

If a future architecture must run an untrusted internal component, that
component must move behind a separate process or service, separate credentials,
and operating-system isolation. That is outside Phase 1. Correcting this threat
model does not relax any external-input, database-integrity, concurrency, or
privacy boundary described below.

## Runtime authority and service boundary

Application startup first creates only a `MemoryReadService`, without a Runtime
Authority, writable Store, or privileged action object. When the separately
gated explicit entry is requested and all startup checks pass, the composition
root calls `bootstrap_memory_runtime_from_environment(...)` exactly once. That
bootstrap invokes the formal deployment configuration loader itself, freezes a
`MemoryRuntimePolicy`, and supplies the process-local authority to the Store.
Repeated bootstrap attempts are rejected without replacing the current
authority, and later environment mutation cannot change the frozen policy.
These are application wiring and misuse controls within the trusted process,
not a sandbox for arbitrary same-process Python.

The bootstrap also creates an independent random action HMAC secret. It is
generated anew for every process, is separate from the fingerprint secret, and
is never placed in configuration, SQLite, logs, errors, readiness, or object
representations. With the explicit entry disabled or invalid, the application
retains only `MemoryReadService`, backed by a separate read-only query object
whose object graph contains neither the writable Store nor Runtime Authority.
With the entry fully valid, only the reviewed internal backend and its four
bound facades retain `PrivilegedMemoryActions`. The runtime authority and
privileged object are never placed in a route, `app.state`, Telegram, Kelivo,
Operit, Galatea, or another adapter.

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
binding version using unambiguous canonical JSON and the process-lifetime action
HMAC. The capability is never returned to an ordinary caller and cannot
survive process restart. Production issuance and verification read
`time.monotonic_ns()` internally; callers cannot provide a clock. The fixed
Phase 1 lifetime ceiling is 30 seconds. Verification rejects malformed,
negative, Boolean, non-integer, oversized, reversed, overlong, future-issued,
or expired time bounds. Exact issue-time and expiry-time boundaries are valid.

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
grant. Missing, forged, changed, not-yet-valid, expired, cross-purpose,
restarted-process, or already consumed capabilities fail with stable data-free
categories and do not write Memory state.

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
MEMORY_EXPLICIT_ENTRY_ENABLED=false
MEMORY_SENSITIVE_STORAGE_ENABLED=false
MEMORY_MAX_ITEM_CHARS=1000
MEMORY_FORGET_RETENTION_POLICY=tombstone_without_content
MEMORY_FINGERPRINT_KEY_ID=
MEMORY_FINGERPRINT_HMAC_SECRET=
```

When Memory Core is disabled, no HMAC key is required and `/readyz` reports
`memory_core=false` without making the service unready. Disabled startup applies
and strictly validates core v1–v6; it neither requires nor repairs optional
v7–v8.
The shared core validator checks every v1–v6 migration marker, table/column,
primary key, default, CHECK, foreign key, unique/partial index, explicit index,
and trigger set. A recorded migration with a missing or changed core object
returns only `core_schema_invalid` and is never silently repaired by
`CREATE TABLE IF NOT EXISTS`. Optional v7–v8 damage remains isolated only while
Memory is disabled.
Enabled read-only mode atomically applies/validates v7–v8 without requiring a
key.
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

### Phase 1.5 action request ledger foundation

Migration v8 is additive and adds only `memory_action_requests`, its explicit
lookup index, and unconditional `BEFORE UPDATE` / `BEFORE DELETE` immutability
triggers. It does not rebuild or modify v1-v7 tables or their data. The
validator requires the exact two-trigger set and normalized SQL fingerprints;
missing, modified, or additional ledger triggers fail closed. Terminal rows
are inserted once and have no update path. The ledger remains a terminal
request record for a later reviewed explicit action entry service; this change
does not implement that service or connect a CLI, MCP tool, HTTP route,
Telegram command, Operit command, or other transport.

Each row binds a bounded server-issued request ID to a closed action kind and
server-owned origin using a 32-byte HMAC-SHA-256 digest. The digest codec
covers request ID, action kind, origin, target public Memory key, scope, Memory
kind, sensitivity, normalized-content representation and version, and the
canonical-action contract version. It reuses the dedicated Memory fingerprint
HMAC secret only after write configuration is valid, under the independent
domain `memory-entry/request-binding/v1`. Domain separation prevents a ledger
digest from being substituted for a Memory fingerprint or profile check while
avoiding another long-lived production secret. A plain content hash is never
used.

Before a terminal row commits, the same request fields are authenticated again
under `memory-entry/request-terminal/v1` together with status, fixed result
category, public result Memory key, canonical message reference, both UTC
timestamps, the outcome/snapshot contract versions, and a versioned
`TerminalSemanticSnapshotV1`. The terminal category and result key are not
caller inputs. After the Store savepoint finishes, `MemoryStore` records a
frozen `StoreOutcomeSemanticsV1` containing only the action kind/outcome,
target/result item references, current evidence/source references, exact
outcome-relevant suppression IDs, and contract version. That replayable value
is wrapped in one sealed, live `TrustedStoreOutcomeV1` owned by the current
Unit of Work. The envelope additionally binds the owning UoW token, Store
object, request ID, canonical message ID, and capability/action ID.
`complete_request()` accepts no category or result-key parameters, requires the
envelope's action ID to equal the sole deferred action, and applies the closed
mapping from its semantics to the terminal row. Unknown, cross-action,
duplicate, missing, cross-UoW, cross-Store, cross-request, cross-canonical, or
cross-action-ID outcomes fail closed.

The typed snapshot is built from the
actual rows inside the same outer `BEGIN IMMEDIATE`: normalized canonical
text and server-owned channel/source projection; action evidence ownership and
binding fields; every related Memory source; target/result item state, scope,
kind, sensitivity, content-presence and supersession; and the exact
outcome-required suppression rows. Remember/correct suppression binds the
matching requested/replacement fingerprint, correction binds the
`corrected_obsolete` row, and both `forgotten` and `already_forgotten` bind the
target's `user_forget` row. The snapshot value is neither persisted, logged,
returned, nor placed in an exception; only its terminal HMAC digest is stored.

Initial completion first revalidates the live envelope's exact type, seal,
owning UoW/Store identities, request/canonical identities, semantics object,
and action ID against the sole deferred capability. It then verifies the actual
Store semantics against the claimed request and already-matched Store
capability before inserting the terminal row.
Envelope ownership/state mismatches raise the fixed data-free `invalid_state`
category; Store/snapshot semantic mismatches raise the fixed data-free
`terminal_semantics_invalid` category. Both paths roll back canonical,
evidence, item, source, suppression, profile, and ledger state. Replay rebuilds
the same typed snapshot from current rows,
recomputes the terminal HMAC, compares it in constant time, and validates the
same ownership and relationship invariants. Canonical, evidence, source, item,
suppression, result-reference, deletion, or addition tampering therefore
fails closed without executing a new request. Replay first reads a strict
`StoredTerminalRowV1`, compares its actual `request_id`, `action_kind`,
`origin`, and `target_memory_key` to the caller binding field by field, and
then rebuilds the terminal payload from the database row's actual canonical,
status, result, category, and timestamp columns. Caller request columns are
never substituted for stored request columns during terminal HMAC
recalculation. Replay reconstructs only `StoreOutcomeSemanticsV1`; it never
creates a live owner-bound envelope and needs no UoW/Store owner token.
Immutable triggers protect normal writes; the HMAC and semantic
snapshot still detect row changes after a synthetic offline
drop/tamper/restore of those exact triggers.

The ledger stores no Memory or canonical text copy, fingerprint, external
user/device/session identity, complete metadata, capability, signature,
Runtime Authority, Secret, Key ID, SQL, or exception body. It has no persisted
`processing` state: a new request is claimed while an internal
`BEGIN IMMEDIATE` owns the SQLite write lock, and only a fixed terminal
`completed` or `failed` row can commit. The exact action/target/result/status
combinations, digest shape, timestamps, foreign key, primary/unique keys,
index, migration marker, and normalized DDL are validated fail closed.
PR A intentionally exposes no helper that persists deterministic input
validation failures: those return a fixed data-free category before canonical,
ledger, profile, evidence, item, source, or suppression state is created.

`MemoryStore` now also has a private transaction-aware foundation for a future
composition-root service. The Store creates the Unit of Work from its fixed DB
path and existing Runtime Authority; no caller supplies a connection, SQL,
path, capability, or policy flag. The Unit of Work owns one root
`BEGIN IMMEDIATE`. Existing Store operations reuse that connection through a
private savepoint, retain all policy/capability/provenance checks, and verify
that the actual Store capability binding exactly matches the claimed request
action, canonical reference, target, scope, kind, content, and sensitivity.
One-use capability completion is deferred until the root commit. A known
rollback releases the capability reservation; an uncertain commit burns the
capability and requires later request-ID lookup rather than blind replay.

This Unit of Work is an application-composition and database-atomicity
mechanism inside the trusted Python process. Its private names, per-UoW owner
token, connection ownership, and exact object checks prevent reviewed
composition-path miswiring and cross-owner transplantation; they are not a
sandbox against arbitrary malicious same-process Python execution.

The first independent review's High 1 / Medium 1 findings were the mutable
terminal ledger and incomplete authentication of referenced terminal
semantics. Later targeted revisions added immutable terminal triggers,
referenced-semantic authentication, real Store-outcome mapping, and replay
validation of actual request columns. The following review closed those
findings but found one new Medium: the live Store outcome did not bind its
owning UoW/Store/request/canonical identities or recheck its action ID at
completion. This targeted revision separates replayable
`StoreOutcomeSemanticsV1` from the owner-bound live envelope and revalidates all
five identities before snapshot construction. The owner token and Store
reference remain process-local: they are not persisted, hashed, logged,
returned, or reconstructed on replay. A later focused review found a separate
Forget-target ownership gap. The entry backend now obtains the exact typed
metadata object from the Store inside the outer UoW; the Store immediately
registers that object with a process-local UoW registry. Claim seals the
registration to the exact Store/UoW identities, all 11 target fields, target
key, `forget` action kind, request ID, origin, and request-binding digest.
The privileged action can retrieve only that registered object, and the Store
compares all 11 fields again against its current write-side row. Replacement,
mutation, stale, unregistered, cross-Store, cross-UoW, cross-request, and
cross-origin objects fail closed. Registration is cleared on commit, rollback,
and uncertain close; it is never persisted, hashed into terminal state,
returned, represented with target data, or logged. It is created only after a
same-transaction terminal probe proves that the request ID is absent and the
request will enter the new-write path. Completed Forget replay and uncertain
terminal lookup never create, seal, consume, or depend on a registration.
The PR remains Draft and is not ready for merge or deployment.

Phase 1.5 PR A adds no production Memory write entrypoint. Core read-only mode
can apply and validate v8 without a fingerprint secret; explicit writes remain
disabled by default. Old v7 application code ignores the additive v8
marker/table. Application rollback therefore leaves v8 in place; a physical
schema downgrade still requires restoration of a consistent pre-v8 backup,
never manual marker or table deletion.

### Phase 1.5 PR B explicit action entry

PR B adds typed, frozen, slotted, representation-safe request/result contracts
and an internal `ExplicitMemoryActionService`. Four composition-root factories
bind provenance permanently:

- `operator_cli` to `channel=web`, `source=relay`;
- `mcp` to `channel=relay`, `source=mcp`;
- `telegram` to `channel=telegram`, `source=telegram`;
- `operit` to `channel=operit_share`, `source=operit`.

The names describe future ownership only. PR B connects none of these facades
to a CLI, MCP server, HTTP route, Telegram command, Operit command, provider,
model, outbox, or external-message path. Callers cannot supply or override
origin, channel, source, canonical ID, result category, result key, Store
outcome, or suppression semantics. This is trusted same-process miswiring
control, not a Python sandbox.

The reviewed internal entry backend owns one outer `BEGIN IMMEDIATE`. Forget
first probes `memory_action_requests` by request ID, fixed origin, and public
target key. A present completed Forget terminal is authenticated from its
actual stored request columns, current content-free tombstone,
canonical/evidence/source/suppression semantics, and terminal HMAC. The
tombstone supplies the persisted scope, kind, and sensitivity used to
reconstruct and exactly validate the full binding. Replay then commits without
target preparation, registration, canonical insertion, capability issuance,
or Store execution. If the probe is absent, the same UoW performs the single A
target projection, registers its exact object, constructs the binding, rechecks
the ledger row in `claim_request()`, seals ownership only after that row is
still absent, and proceeds through canonical insertion, Store savepoint,
terminal validation/insertion, and outer commit. `BEGIN IMMEDIATE` keeps the
probe-to-claim sequence single-winner.

A definite rollback releases the capability; an uncertain commit burns it,
closes the current UoW, and performs only a fresh same-binding
existing-terminal lookup. That lookup never enters the new-request claim path.
A present terminal returns replay; an absent terminal returns
`transaction_outcome_uncertain` and is never blindly re-executed.

Remember canonical text is normalized explicit user content. `decision` is
always routed to confirmed project decision; `assistant_experience` is
rejected. Correct canonical text is normalized replacement content and targets
only a public Memory key. Forget uses only
`Forget explicit memory: <public memory_key>`. Its request/capability binding
has `normalized_content=None`. Target resolution and the Store action use
explicit metadata-only projections: active plaintext is never selected,
materialized into a Python/SQLite result row or dict, or copied. The terminal
snapshot reads only tombstone metadata plus SQL `IS NULL` absence flags; it
never returns plaintext or fingerprint values to Python. Canonical data,
results, errors, representations, readiness, and logs therefore contain no
forgotten plaintext. Store state, tombstone, suppression, and the authenticated
terminal snapshot are sufficient for restart replay.

Every Forget-path `memory_items` read is one of three exact projections. A is
the 11-field prepare/registration projection and runs exactly once only for a
new request after the terminal probe is absent. B is the Store projection:
active Forget reads it once before and once after the update, while
`already_forgotten` reads it once. C is the content-free tombstone projection
with only metadata and `IS NULL` absence flags; completion and every completed
replay read it once. The resulting `memory_items` sequences are
`A -> B(key) -> B(id) -> C` for new active Forget,
`A -> B(key) -> C` for a new already-forgotten request, and only `C` for
same-process replay, fresh-runtime replay, real process-restart replay, and
uncertain terminal lookup. Forget completion trusts the internal
Store-produced item ID already bound to the registration and B row, while
replay uses C's validated item ID, so neither path performs a separate
`SELECT id`. The tombstone query has no self-join, and source semantics query
only `memory_sources`, projecting `memory_id -> memory_key` from already
validated terminal items. A SQLite authorizer-backed test gate uses no
statement-keyword or `SELECT`-presence shortcut. Every `memory_items`
authorizer event must belong to an exact A/B/C projection, the exact
content-clearing Forget UPDATE, or the exact `memory_sources` insert whose
SQLite foreign-key check reads only `memory_items.id`. The gate separately
validates read/update columns, cursor description, completion state, and a
conservative raw-statement key. That key normalizes only line endings,
token-external whitespace, and spacing around safe punctuation; it preserves
every literal, parameter placeholder, quoted identifier, comment, clause, and
expression. In particular, literal Memory keys and integer IDs are never
folded into `?`. Unknown literal A/B lookalikes, quoted, comment-adjacent,
schema-qualified, CTE, alias, join, subquery, UPSERT, trigger, and
write-`RETURNING` bypasses are rejected. Persistent gate violations and
records contain only fixed categories, registered names or `unknown`, safe
schema column names, descriptions for registered statements, and booleans;
they retain no SQL, statement key, database path, trigger text, parameter, or
literal. The legitimate Forget UPDATE has no `RETURNING` and produces no row.
Restart coverage uses two independent `sys.executable` module subprocesses
over one temporary SQLite database, not an in-process runtime bootstrap.
Their stdin JSON is bounded to 16 KiB and validated against an exact
phase-specific schema before runtime construction. Tests reject stdout,
stderr, JSON, repr, argv, and error leakage of plaintext, the synthetic HMAC
secret, its fingerprint, internal registration/UoW type names, or object
addresses. Permanent tests also cover same-request Forget with 2/4/8
independent SQLite callers and the complete tombstone/suppression tamper
matrix. Real completed-replay SQLite cases cover every tombstone semantic
field (`id`, key, status, kind, scope, sensitivity, explicitness, confidence,
fingerprint version, updated time, content/fingerprint absence, and
supersession), self/dangling/valid-target supersession, and row deletion.
Suppression cases cover every field, deletion, and structurally valid
replacement. Every replay fails closed without A, registration, capability,
Store execution, terminal mutation, or business-table growth.

`MEMORY_EXPLICIT_ENTRY_ENABLED=false` is the default. False constructs no
entry backend, service, facade, authority, or writer. True additionally
requires Core, explicit writes, valid key ID/HMAC configuration, a valid
fingerprint profile, and exact v7/v8 schema. Failure constructs no entry writer
and reports the independent data-free `memory_explicit_entry` readiness check;
entry-only configuration faults do not change `memory_core` readiness.

PR B changes no DDL or migration tuple: `migration_v9_needed=false`. Production
remains read-only, with Core/writes/entry activation and real Memory Secret
configuration outside this PR's approval.

## Operator preflight and composition API

`backend.memory_operator_composition` provides a non-FastAPI composition root
for a future operator CLI. It does not implement a CLI command or create an
HTTP, MCP, Telegram, Operit, provider, network, or outbox path.

`preflight_operator_memory_from_environment(telegram_config, environ=None)`
loads one frozen `DeploymentConfig` and then, without constructing a Runtime
Authority, Store, writer, backend, or service:

1. requires Memory Core, explicit writes, and explicit entry to be enabled and
   valid in that frozen snapshot;
2. requires the configured SQLite file to exist;
3. opens it through a `mode=ro`, `query_only=ON`, foreign-key-enabled
   connection using the frozen busy timeout;
4. validates the exact v1-v8 marker set, complete core/relay/v7/v8 schema, and
   the configured fingerprint profile;
5. rejects attached databases and compares every application-defined
   table, view, index, and trigger in `main.sqlite_schema` with the exact
   object set derived from the authoritative v1-v8 DDL registries; and
6. closes the connection and returns a frozen, slotted, representation-safe
   `MemoryOperatorPreflightV1`.

The v1-v8 validator rejects missing, duplicate, renamed, non-applied, or
unknown markers, including any v9+ marker. Profile validation shares the
connection-aware rules used by `MemoryReader`: an absent profile is accepted
only when all Memory and action-ledger business state is empty; every mismatch
returns `memory_fingerprint_profile_mismatch` without exposing the Secret,
key check, fingerprint, path, SQL, or stored data. SQLite-owned names under
the reserved `sqlite_%` namespace, including `sqlite_sequence` and automatic
indexes, are excluded from the application object set; no application view is
currently approved.

`compose_operator_memory_service_from_environment(...)` runs that same
preflight against the same frozen deployment snapshot. Only after it succeeds
does the function enter a bootstrap-lock-protected pending runtime scope,
create the reviewed entry backend, and bind `operator_cli`. The process
Authority is published only after the exact `ExplicitMemoryActionService` is
fully constructed. Failure during action-secret, Store, reader, privileged
writer, backend, or binding construction invalidates only that exact
unpublished Authority, leaves the process unbootstrapped, and permits a safe
retry. `KeyboardInterrupt`, `SystemExit`, and other `BaseException` values are
not translated into business failures, but the pending runtime is still
cleaned. A successfully published runtime cannot be reset or rolled back by
this mechanism. The function never binds MCP, Telegram, or Operit and does not
separately return Runtime Authority or privileged actions or expose them
through the facade's public API, representation, or logs. The exact service
necessarily retains its reviewed backend object graph under the existing
trusted-same-process threat model; this is a composition and misuse boundary,
not a Python sandbox.

The existing `*_from_environment` runtime bootstraps remain available and now
delegate to exact-type frozen-`DeploymentConfig` variants. This change adds no
DDL or migration: `migration_v9_needed=false`.

The object allowlist does not claim an atomic binding to the preflighted file
identity. A local actor with database-directory write access can still replace
the database, symlink, WAL, or SHM between preflight and a later action. That
TOCTOU remains an accepted residual risk of the existing trusted-host threat
model.

## One-shot operator CLI

`python -m backend.memory_operator_cli <command>` is the only command-line
entry point for the operator composition root. It is a one-shot, local process;
it does not import `backend.app`, construct FastAPI, listen on a socket, start a
worker, register a route or tool, or call a provider, model, network transport,
or outbox. It is not installed in App state and is not reachable from chat,
MCP, Telegram, Operit, Kelivo, or Galatea.

The exact commands are `remember`, `correct`, `forget`, `status`, `validate`,
and `generate-request-id`. Commands accept no business arguments. The three
write commands read one strict UTF-8 JSON object from binary stdin, bounded to
32 KiB, and reject BOMs, duplicate keys, non-finite numbers, trailing values,
missing or additional fields, and non-exact field types. Memory content,
replacement content, scope references, and Secrets are never accepted through
argv. `status`, `validate`, and `generate-request-id` require empty or
whitespace-only stdin.

After argv validation and the bounded stdin parse,
`generate-request-id` dispatches immediately. It calls the formal request-ID
issuer exactly once, before reading or validating Telegram, Memory, database,
Kelivo, Loop, Heartbeat, Secret, or path configuration. It is therefore a
genuinely offline operation and remains available when any of those unrelated
environment values are missing or malformed.

Every other command requires the one-shot operator environment to set
`TELEGRAM_ENABLED=false`. Invalid strict-boolean syntax or `true` fails as
`readiness_failed`. The CLI constructs the formal disabled `TelegramConfig`
from only that fixed setting; caller-supplied Telegram API bases, allowlists,
and test-mode values are deliberately ignored. Consequently no custom
hostname is resolved and the operator path performs no DNS, socket, HTTP,
provider, transport, or outbox operation.

Write commands call only
`compose_operator_memory_service_from_environment(...)` and then one method on
the returned origin-bound operator service. They do not call preflight
separately or directly access Runtime Authority, Store, privileged actions,
capabilities, unit-of-work objects, or SQL. The C0 projection remains fixed to
`operator_cli -> channel=web, source=relay`. `status` loads the formal
deployment configuration once without opening SQLite; `validate` performs only
the public read-only operator preflight; `generate-request-id` calls only the
formal request-ID issuer and does not require Memory activation.

Every invocation emits exactly one single-line, seven-field JSON object on
stdout. Failures also emit exactly one fixed ASCII public category on stderr
and use the frozen exit-code mapping: input 2, readiness 3, request-binding
conflict 4, not-found/unsupported action 5, storage unavailable 6, uncertain
transaction outcome 7, and internal error 1. Unknown internal categories fail
closed as `internal_error`; exception text, tracebacks, input content, paths,
SQL, and object representations are never public output.

The CLI does not create or repair a database, run migrations or recovery, or
change production activation. Operators must provision an existing validated
v1-v8 SQLite database and explicitly enable Core, writes, and entry in the
one-shot local process environment. Each process accepts exactly one command;
batching multiple actions in one process is unsupported. This code does not approve a production
Memory Secret, production writes/entry, or deployment.
`migration_v9_needed=false`.

## Deferred phases

Phase 2 may add model-assisted candidate extraction with an independently
audited provider call and fail-closed activation policy. Phase 3 may add
bounded, policy-controlled retrieval and prompt context. Neither capability is
implemented or enabled by this change.

## Review status

Phase 1.5 PR B is an internal, default-disabled entry composition. It does not
approve deployment, production Secret configuration, Core/write/entry
activation, or a transport. It must remain Draft until independent security
review completes. Exact test totals remain pending the new exact-head CI run.
