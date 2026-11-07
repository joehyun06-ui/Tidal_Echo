# GM-02T: fixed history-count setting packet

PrivateHistorySetting defaults off and binds only loop.update.v1 with global
target None and exactly history_n, an integer 0..200 (bool refused). It cannot
change routes, models, credentials, sessions or dispatch. Retention freezes the
caller value/request ID, not config, environment routes or credentials. Prepare
checks current global command permission and separate packet ACL but does not
read canonical config or environment routes, reserve state or create SQL intent.

Execute checks exact packet/reference/actor/resource, concrete dependencies,
shared session lock and named DB/config/gate namespace, then calls only the
existing update_config. Actual connection pin and current authorization are
checked by the existing coordinator. Prepared-only recovery refuses missing
intent; pending recovery publishes original staged JSON without reapplying the
mutator; committed replay preserves later edits. Current server render policy
and fallback identity are resolved by the original command, not frozen by
prepare. Changed policy/fallback versus an existing intent refuses recovery;
there is no credential snapshot or automatic compensation.

This is explicit last-writer setting semantics, not old-draft CAS. It preserves
omitted config via the original command; history_n affects future history
selection, not deletion of stored messages or proof of model consumption.
Global target None is the bound config resource, not arbitrary resource access.
No HTTP, automatic recovery, native runtime, model migration or global route
editor is installed. Trusted filesystem/256KiB/fsync/orphan-stage, SQL/JSON
non-atomicity, named-path versus inode/anti-rollback and external-writer limits
remain. Production HOLD; all integration flags remain False. G3 truth, derived
ProjectionCard and frozen 36/36,108/108,historical OpenAI7/7 unchanged.
