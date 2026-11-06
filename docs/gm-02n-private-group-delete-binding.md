# GM-02N explicit private-packet API group-delete composition

Default-off offline continuation of GM-02M. PrivateApiGroupDelete composes the
existing GM-02L concrete command and GM-02M private storage. It is not a generic
dispatcher, credential, HTTP endpoint, startup task or automatic recovery agent.
The default legacy/HTTP/native paths and production integration flags are unchanged.

prepare takes the root, stable request ID, explicit UTC deleted_at, explicit
fallback and current trusted access. Under the exact command session RLock it
obtains a real query-only whole-group snapshot, authorizes write access to every
discovered original member and fallback, and retains the exact fixed packet.
prepare does not invoke erasure or create a canonical intent. Its PacketReference
must be durably retained by the trusted caller before execute. If preparation
results are uncertain, retry the identical request while preflight remains
unchanged; it must not silently generate replacement IDs or new preconditions.

execute takes only the root, original reference, current trusted access and an
explicit bool recover flag. It reads and checks the private original packet,
then invokes only CoordinatedApiGroupDeleteCommands.delete_api_group. Stored
fields cannot supply access/recover, arbitrary method names or extra keyword
arguments. Schema, contract/root/request identity and exact precondition keys
are checked; the concrete command independently validates all field values,
all original member/fallback scopes, CAS, current resource and effect proof.

Recovery never obtains another snapshot or rediscovers membership from deleted
edges. A prepared packet without an intent cannot be recovered; normal execute
remains the explicit way to start it. Pending recovery requires the original
packet and refuses changed state/new ingress, never re-erasing it. Post-JSON
recovery and committed replay retain the existing historical receipt semantics.
No new current/global deletion or runtime-success claim is introduced.

The adapter requires exact concrete dependency types and the same injected
RLock. Before packet IO it requires the storage namespace to equal the existing
coordinator binding digest of the absolute resolved canonical DB/config/gate
paths. Actual SQLite identity is still enforced by the concrete command and
coordinator. This binds this assembly to named local resources, not a new
cryptographic trust root, external ACL transaction or anti-rollback mechanism.

Global/root command authorization precedes private-packet access; snapshot
read access and complete-group write authorization precede retention. Storage
authorization and the command's independent full-scope authorization both
remain necessary. Authorization revocation can leave a retained packet or a
committed pending erasure; it does not grant recovery or undo committed SQL.
Private packet storage must remain trusted/private with the protections and
limitations documented in GM-02M, including reference retention, 256 KiB cap,
uncertain fsync results, transient link-state refusal and orphan stages.

Private storage and canonical SQL are not one transaction. Persisting a packet
does not reserve the group or prevent later history/metadata changes. Stale
preflight loses the original command's CAS and must never be upgraded silently.
All GM-02L unsupported references, logical erasure limits and SQL/JSON/runtime
gaps remain. This is one explicit API composite adapter; other commands,
production auth/HTTP, read/write/ingress/runtime fences and native composition
remain future work. No automatic resume, packet listing or human-intent claim.

Synthetic tests compose actual nested version groups and private files. They
cover exact prepare/no effects, whole-group authorization, namespace/lock
binding, current packet and command authorization, fixed-packet execution and
replay, stale content, malformed identity/schema, file permissions, process exit
after durable SQL intent and re-opened exact recovery without a graph, pending
new ingress/revocation refusal and post-publication preservation of later data.
No real user content, model calls, production file writes or G3 mutation occurs.

Keep production_supported/http_integrated/automatic_recovery_supported False.
No merge/deploy/restart or production DB/config/env/WAL/Journal change.
ProjectionCard stays authority=derived, commercial C remains outside daily
operations, frozen 36/36, 108/108 and historical OpenAI 7/7 unchanged. Production HOLD.
