# GM-02P private packets for API branch and version-selection commands

Default-off continuation of GM-02O. Two explicit adapters compose the existing
GM-02H plain API branch command and GM-02J SQL-only version selector with GM-02M
private original packets. A small internal helper shares admission, named
resource binding and envelope checks. There is no public operation registry,
body-supplied method name, arbitrary keyword dispatch, HTTP route, startup scan
or automatic recovery. Existing GM-02N/O/default legacy callers are unchanged.

## Branch preparation and recovery

PrivateApiFork.prepare accepts a plain unversioned API source, fixed UUID/body
point/mode=branch, explicit title/UTC created_at and trusted current access.
The existing parser normalizes UUID spelling and fixes api-fork-UUIDhex as both
target and operation/packet request ID. Source and target write scopes precede
observation; real query-only preflight and final write guards precede retention.
The snapshot binds original generation/config/history/copy, never raw history
or route secrets. No target row, messages, context, fingerprint or intent is
created during prepare. The trusted caller retains source/reference first.

execute loads that exact packet, validates contract/source/key set, all snapshot
fields and canonical UUID target == reference ID, then calls only the existing
fork_conversation command. Body-supplied access/recover/dispatch is rejected.
Current source/target authorization, real DB identity, CAS and pending copy/
context/fingerprint proof remain independently checked by the original command.
No new preflight, allocation, clock, inference, prompt resend or fork-copy pass
is introduced on recovery. A prepared-only packet cannot recover/start a fork.
Pending new target input refuses publication without purging it; after JSON
publication recovery/final replay preserve later branch history. The explicit
branch can create its intended separate logical chat, but never activates it
implicitly. No real user chat is created by the synthetic tests.

The underlying GM-02H restrictions remain: versioned/native/mixed sources and
unsupported state fail closed. Attachment metadata may be retained as existing
references, but no external attachment files are copied or accessed here.

## Version-selection preparation and recovery

PrivateVersionSelection.prepare takes a logical API root, version_id body,
stable explicit request ID and trusted current access. Root/target write scopes
precede observation; actual snapshot_selection discovers and authorizes the
bounded original group. Every original member must also authorize writing
before the exact graph/config/generation/group snapshot is retained. No
selection, message/config byte, schema or canonical intent changes in prepare.

execute checks original contract/root/request ID/key set/preconditions and calls
only select_version. The concrete command rechecks the complete current graph,
all original scopes and CAS. Selection/generation/committed receipt remain one
canonical SQL transaction; no JSON publication or message copy occurs. A stale
graph refuses; no silent target change or newer preflight can replace a retained
request. Prepared-only recovery cannot start selection. Reopened committed
replay is historical and preserves a newer subsequent choice; it does not
reactivate the old choice or prove runtime context consumption.

## Shared boundaries and verification

Both require the exact concrete command type, current command and storage
authorization, the same injected session RLock, and a namespace matching the
existing coordinator's digest of absolute resolved canonical DB/config/gate
paths. Current global/root authorization precedes packet IO. Cross-action use
of a reference, actor/root/hash/schema changes and fake packet-supplied grants
refuse before command mutation. Actual SQLite binding remains checked by the
command/coordinator; no new database or credential issuer is added.

The existing browser-login/session modules were inspected: they explicitly
remain offline-review candidates, depend on independently authenticated login,
and cannot be promoted into configuration-command authority. No auth module,
login token, epoch, production route or grant is changed in this step. Actual
authentication, scoped authorization and atomic revocation fencing remain
independent production gates; ConfigCommandAccess is still not a credential.

Synthetic tests use contained real branch/selector fixtures rather than exposing
old TestCase classes to duplicate discovery. They cover both admission paths,
no canonical effect during prepare, fixed fields and UUID identity, full scopes,
same lock/resource, independent storage/command authorization, cross-action
reference refusal, envelope/key/identity conflicts, private file modes, original
CAS, branch replay/context copy, SQL-only selector, actual process exit/reopen
recovery, pending branch ingress refusal and preservation of later branch input
or a newer selection. Child execution blocks socket connects; no model calls
or live user content are used.

All GM-02M private storage/reference/256 KiB/durability/orphan-stage limits and
GM-02H/J external-state/unsupported-path limits remain. Private retention is
not atomic with canonical SQL and reserves nothing. Changed preflight must
refuse, not be silently replaced; authority loss/damage may leave packets or
pending copies for authorized reconciliation. There is no automatic adoption,
cleanup, compensation, packet discovery or human-intent proof.

Keep production_supported/http_integrated/automatic_recovery_supported False.
Other commands, real auth/HTTP/native, all-writer/read/ingress/runtime fences,
consumed model/source/window evidence and production backup/recovery remain
open. No merge/deploy/restart or production DB/config/env/WAL/Journal change,
model API/G3/automatic memory write, real chat or message action. ProjectionCard
authority=derived; commercial C outside daily operations; frozen 36/36, 108/108
and historical OpenAI 7/7 unchanged. Production HOLD.
