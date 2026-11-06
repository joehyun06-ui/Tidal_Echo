# GM-02S: private single-session patch packets

Default-off offline adapter `PrivateSessionPatch` binds exactly the existing
`CoordinatedConfigCommands.patch_session` and `session.patch.v1` action. It adds
no HTTP installation, runtime switching, background recovery or production support.

Prepare retains a nonempty explicit subset of title (string), pinned (bool) and
active (bool), with stable caller request ID and target. Input is deep-frozen.
No provider, settings, method, access or recover field can be supplied in the
body. The retained packet contains no full config, fallback routes or secrets
from the environment. Caller-supplied title text is private and is not sanitized
as a credential detector. The private packet is not a credential or proof of intent.

Prepare authorizes current target write access and private storage separately,
but does not read session metadata, reserve a target, check existence or publish
canonical intent. Existence/tombstones/provider validation occurs in the original
command at execution time; a prepared packet is not an executability promise.

Execute accepts only target/reference/current trusted access/recover bool, checks
the exact envelope and body, and calls the fixed original patch command. All
three concrete dependency types, same session lock, resolved absolute DB/config/
gate namespace and explicit canonical connection pin are required. Actual SQL
connection pin verification remains inside the original coordinator. Current
command and storage permission are rechecked, including replay and recovery.

Prepared-only recovery refuses missing canonical intent. After pending intent,
explicit recovery uses original staged JSON without re-running patch_session.
Committed historical replay does not undo later edits. Original command policy
(including render_mvp) changes can conflict with saved intent, rather than being
silently substituted. Recovery is never an automatic retry of an unknown send.

## Deliberate inherited boundary: no draft CAS

The original single-field patch command has no expected-generation/snapshot
input. This adapter does not invent one: `draft_cas_supported=False`. Before the
first execution, an explicitly supplied title may overwrite a later title;
omitted fields are merged against current config by the original authority.
active=False means no active-selection change, not deselecting a session.
This is not GM-02R's original-snapshot bulk draft protection. Callers requiring
stale-draft rejection must use the existing bulk metadata CAS path. Neither path
is installed into HTTP or a user editing flow here.

API/Codex existing metadata may be patched; this does not bind or switch native
runtime. GM-02D normalization/raw metadata behavior is inherited, not replaced by
the GM-02R bulk merge. No all-writer, native, source-consumption, read/ingress,
external ACL atomicity or production backup guarantee is added. The named-path
pin is not inode pinning or anti-rollback. Private packet trusted-filesystem,
256 KiB, unknown-fsync and orphan-stage limits remain. Global settings and model
migration adapters remain separate work. Production HOLD; G3 truth and frozen
36/36,108/108,historical OpenAI 7/7 unchanged.
