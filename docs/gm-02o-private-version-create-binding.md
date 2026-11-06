# GM-02O explicit private-packet API version-create composition

Default-off offline continuation of GM-02N. PrivateApiVersionCreate composes
GM-02K's concrete API edit/regenerate command with GM-02M's private original
packet store. No generic dispatcher, HTTP wiring, automatic resume, inference,
prompt resend or legacy prepare/fork/finish chain is installed.

prepare receives the logical root, source version, explicit UUID/message point/
mode body, title, explicit UTC created_at, select bool and current trusted access.
The existing concrete request parser normalizes UUID spelling and fixes the
api-fork-<uuid hex> target. This target is also the existing canonical operation
ID and the private packet request ID; no new ID/time allocation is introduced.
Root/source/new-target write scopes precede observation; actual query-only
preflight authorizes read access to the original group, and the complete
original group/new target must also authorize writing before packet retention.

Under the exact command session RLock, prepare retains contract/root/source,
normalized request body, explicit title/time/select and all original snapshot
preconditions. It does not create a version edge, copy messages, change selection,
publish JSON or write canonical intent. The trusted caller must durably retain
the reference and root before explicitly executing. The reference alone does
not authorize creation or prove human confirmation.

execute accepts only root/reference/current access and explicit bool recover.
It loads the original private envelope, checks contract/root/exact keys and
precondition fields, revalidates the body and requires its canonical target to
match the reference request ID. No packet-supplied access/recover/method or
arbitrary keyword dispatch is accepted. Only create_api_version is called.
That concrete command independently rechecks all original group/target scopes,
actual resources, CAS and pending effect proof. Title/time/mode/point/select
changes cannot reuse a retained request ID; UUID textual aliases normalize to
one operation identity rather than allocating another target.

Binding uses the existing coordinator digest of absolute resolved canonical
DB/config/gate paths, exact concrete dependency types and same RLock. Actual
SQLite identity remains enforced by the command/coordinator. This named local
resource binding is not a credential, signature, external ACL fence or rollback
proof. Private packet limits/protections remain those of GM-02M.

Recovery does not call snapshot_version or recopy changed source history. A
packet with no intent cannot be recovered as if it had started; execute remains
the explicit start operation. Before JSON publication, the original concrete
effect validator requires the exact copy/edge/fingerprint/selection and target
namespace, rejecting new target input or damage without purging it. select=False
is persisted and preserves the prior stored selection, including absence.
After JSON is published recovery only finalizes; later input and newer selection
remain intact. Committed replay returns its historical receipt, not live UI,
runtime confirmation or evidence of a fresh model invocation.

Private retention and canonical SQL are separate transactions. Prepare reserves
neither the target globally nor the source view; changes before intent lose CAS.
Changed source after intent is not recopied, and pending proof is not an atomic
external source/context view. Authority loss/damage can leave a private packet,
copy and pending intent requiring authorized reconciliation; no automatic undo,
adoption, repair, orphan cleanup or private reference discovery is introduced.
Legacy/native/mixed groups, jobs/tools/namespaces/attachments and other GM-02K
restrictions remain. HTTP/native/other command adapters and all-writer/read/
ingress/runtime fences remain future work.

Tests use real nested legacy API versions and private synthetic files, via
contained fixtures rather than duplicating imported TestCase classes. They
cover query-only canonical preparation, fixed fields and UUID identity,
read/write/group/target authorization, lock/resource binding, packet and command
independent refusal, select=False, nested first-prompt edit, exact replay/CAS,
actor/root/digest/schema conflicts, real process exit after intent and reopened
original-copy recovery, pending new ingress/revocation refusal and preservation
of later input/selection after publication. No model or live user data is used.

production_supported/http_integrated/automatic_recovery_supported stay False.
No merge, deployment, production DB/config/env/WAL/Journal change, restart,
model API call, G3/automatic memory write, actual chat or message action.
ProjectionCard remains authority=derived; commercial C is outside daily
operations. Frozen 36/36, 108/108 and historical OpenAI 7/7 remain unchanged.
Production HOLD.
