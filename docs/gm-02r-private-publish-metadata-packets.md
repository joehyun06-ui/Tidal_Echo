# GM-02R private API-row publication and metadata-draft packets

Default-off continuation of GM-02Q. Two explicit adapters retain the original
GM-02G publish_row request and GM-02F metadata-only save_sessions request.
There is no generic dispatcher, HTTP/startup installation, automatic resume,
credential issuer, model call or native lifecycle operation. Existing adapters,
commands and default legacy callers are unchanged.

## Prepared API-row publication

PrivateApiRowPublish.prepare receives the complete explicit API row, stable
request ID, activate bool and current trusted access. The existing row parser
requires fixed ID/title/UTC created_at, since_id=0, pinned=False and API provider.
Current target write authority and list-snapshot metadata read scopes precede
retention. It saves the exact row/activation and original generation/hash,
without allocating an ID/time, publishing a row or writing canonical intent.

execute takes target/reference/current access/recover bool, checks envelope,
contract/root/request/body/precondition keys and row ID == target, then calls
only publish_row. Existing fresh namespace/tombstone/history/capacity/CAS and
current authority checks remain. Preparation is not target reservation or a
guarantee that execution will find it fresh. Prepared-only recover cannot start
publication. Reopened intent recovery uses the original staged configuration,
not a new snapshot, ID or timestamp; committed replay does not restore an old
title over a newer edit. activate=False remains explicit. This path is not
lifecycle reentry or automatic creation of another visible chat.

## Original metadata draft, never a silent rebase

PrivateSessionMetadataSave.prepare requires rows/optional active plus the
caller's original expected_generation and expected_snapshot_sha from the view
on which edits were made. It validates fixed metadata shape, every requested
row's current write scope, actual metadata read scopes and current snapshot.
If that snapshot differs from the caller's original generation/hash, it rejects
before retaining any packet. It never substitutes a newer hash for an old
draft. Even unchanged row topology must retain the original CAS; otherwise a
concurrent title/pin update could be silently overwritten.

prepare also refuses reordered/added/removed/reidentified/provider/since/created
topology, retaining only the original schema-bounded rows, optional active and
original CAS. No metadata/config byte or canonical intent changes during
prepare. The current 256 KiB private-packet cap can reject a large draft; it is
not silently truncated, split or written as partial replacement.

execute accepts only the original reference/current access/explicit recover,
loads the fixed global target=None contract and invokes only save_sessions.
The existing command again validates the complete original row scopes,
immutable topology, generation/hash and history/provider. It merges only title/
pinned and optional active into the raw existing config, preserving extensions,
routes/settings/tombstones and legacy representations. API/Codex rows can have
their existing metadata edited; this does not create, retire, switch or confirm
native runtime sessions. native_integrated remains False.

Prepared-only recovery cannot start a merge. After intent, recovery uses the
retained original packet and staged bytes without re-preflighting or rebuilding
the draft. Revocation of any original row refuses publication. Once published
and finalized, historical replay preserves newer metadata. A retained packet
does not reserve state: changes after prepare still lose the original CAS.

## Admission, persistence and verification

Both require exact concrete command/store/coordinator types, the same injected
session RLock, an explicit canonical_path pin from GM-02Q and the existing
DB/config/gate namespace. The coordinator checks actual transaction connections.
Current command and storage authorization are independently required; packet
fields cannot provide access/recover/method or alternate identities. Different
actors/actions/targets, schema/hash conflicts, unsafe modes and wrong concrete
dependencies refuse. Hashes/ConfigCommandAccess/path pins are not credentials,
signatures, human-intent proof or atomic fencing with an external ACL.

Tests use contained real creation/metadata fixtures, temporary SQLite/JSON and
private files; old TestCase classes are not exposed to duplicate discovery.
They cover both default-off/no-effect prepare paths, exact inputs, immutable
references, independent authority and safe files, cross-action refusal,
original CAS before and after retention, topology, no silent rebase, oversize
refusal, raw config preservation, mixed-provider metadata, actual child-process
intent crashes and reopen recovery, no new snapshot or duplicate row, current
revocation and preservation of newer edits after historical replay. Child socket
connects are blocked; no real user content or model is used.

GM-02M private reference/filesystem/size/fsync/orphan-stage limits, GM-02Q's
named-path rather than inode/anti-rollback guarantee, and GM-02F/G's external
writer/SQL-JSON/runtime limitations remain. There is no automatic adoption,
cleanup, compensation, packet scanning or source-history rewrite. Global config
settings/model migration/native adapters and actual auth/HTTP, all-writer/read/
ingress/runtime fences, model/source/window consumption and production backup/
recovery remain separate work. No production enablement is implied by passing
synthetic tests.

production_supported/http_integrated/native_integrated/automatic_recovery_supported
remain False. No merge/deploy/restart or production DB/config/env/WAL/Journal
change, model API, G3/automatic memory write, actual user-chat/message action.
ProjectionCard authority=derived; commercial C outside daily operations; frozen
36/36, 108/108 and historical OpenAI 7/7 unchanged. Production HOLD.
