# GM-02U: original model migration and receipt-bound rollback packets

Default-off PrivateModelMigration and PrivateModelRollback bind exactly the
existing GM-02E commands, separate actions/contracts and global target None.
Prepare requires caller original generation/expected model or exact rollback
source request/base generation/previous and migrated models; it never snapshots
or replaces CAS, allocates identities, writes SQL intent, or reads config/routes.
Only model strings and preconditions are retained, not URL/key/full config.
Model strings are private caller input, not automatically scrubbed credentials.

Both require current command and separate private storage ACL, concrete types,
same injected lock, explicit canonical connection pin and named DB/config/gate
namespace. Execute accepts only reference/current access/recover bool; strict
shape and identity checks precede fixed dispatch. The original command checks
generation, model and committed source proof; retained rollback payload alone
is not proof. Current permission and proof are rechecked in pending recovery.

Prepare is not an existence, materialized-route, executability or reservation
promise. Prepared-only recovery refuses; pending recovery uses original staged
JSON without reapplying model mutation. Historical replay preserves later edits.
No environment-route bootstrap, route/key editing, old-file restoration, model
request, runtime restart/probe, HTTP installation or automatic recovery occurs.
Changing a config model is not proof a live runtime/model consumed that choice.

Inherited limits remain: SQL/JSON non-atomicity, trusted private filesystem,
256KiB/fsync unknown/orphan stage, named-path not inode or anti-rollback, external
writers/runtime and non-atomic external ACL revocation. Production HOLD;
production/http/native/automatic recovery flags False. G3 source/event/revision
truth, derived ProjectionCard, commercial C independence and frozen baselines
36/36,108/108,historical OpenAI7/7 unchanged. Global route editor, actual auth/
HTTP/all-writer/read/ingress/runtime fences and production recovery remain open.
