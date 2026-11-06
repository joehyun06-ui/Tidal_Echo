# GM-02M private original request retention

Default-off offline continuation of GM-02L. PrivateCommandPackets retains a
trusted server's explicit command arguments before invoking any existing
coordinated command. It never executes, resumes, schedules or enumerates commands,
never invents a command ID/time/precondition, and installs no HTTP/startup hook.

The server supplies a fixed 64-hex namespace binding, original request ID,
principal/access, versioned action, target and JSON arguments. The namespace
must identify the exact canonical/config/coordinator installation; this module
does not discover or verify that mapping. A request ID is immutable within that
namespace across actors/actions/targets. A changed packet conflicts rather than
overwriting a pending recovery's inputs. A different namespace is independent.

prepare returns only request_id and packet_sha. Preserve this reference through
an independently trusted private channel. load requires the expected SHA and
current server authorization and returns a detached original arguments dict.
It rechecks binding/identity/strict canonical JSON before returning. The receipt
does not contain private arguments and has no content-bearing repr. A digest is
not a signature, encryption, credential, human confirmation or anti-rollback
proof. A privileged writer who can replace both packet and expected digest is
outside this local integrity guarantee. Loss of the reference is not repaired
by blindly listing or dispatching files.

Storage requires an explicitly existing absolute, resolved POSIX directory
owned by the process user with mode 0700. Parent directories/filesystem must be
trusted. The implementation anchors relative IO to its open directory FD,
refuses symlinks, nonregular files, hardlinked files and modes other than 0600,
and limits each envelope to 256 KiB. Constructor/default-off/denied calls perform
no storage IO; no directory is auto-created. No absolute paths or bodies appear
in fixed error categories. Packets may contain secrets and must never be placed
in Git, public logs, shared backups or a user-visible artifact location.

A random private staging filename is written and file-fsynced, then linked
without overwrite to the request's hashed filename. The stage link is removed
and the directory fsynced before success. Existing byte-identical prepare also
repeats the directory durability barrier. An fsync error can leave a published
packet with an uncertain result; retry prepare with exactly the same inputs.
No already-published packet is deleted or overwritten in error handling.
Different concurrent inputs cannot overwrite each other. A concurrent reader
may conservatively refuse the brief two-link publication state and must retry;
there is no promise that every concurrent caller succeeds immediately.
Process exit before publication may leave an orphan private stage; no automatic
stage scanning, adoption, cleanup, retention expiry or secure erasure is added.

Authorization checks precede any IO and repeat before publication/return. The
callback must validate real trusted server identity. ConfigCommandAccess is
not a credential. These are point observations, not atomic fencing with an
external ACL authority. A retained packet grants no permission: existing command
execution/recovery must independently check every original group scope, current
authorization, resource binding, expected parameters, CAS and effect proof.
No body-supplied grant or generic dispatch registry is accepted here.

The offline integration test persists actual config command arguments, crashes
after coordinator pending intent, reopens the packet store and coordinator, then
passes the original packet to the existing guarded recover entry. A separate
real child exits with os._exit after packet publication; a new process can load
the exact retained body. This establishes local recovery material retention,
not production HTTP/auth, group runtime, model consumption or storage backup
acceptance. Group-delete and other composite commands remain separate adapters.

production_supported/http_integrated/automatic_recovery_supported remain False.
No merge/deployment, production DB/config/env/WAL/Journal change, restart, model
call, G3/automatic memory write or actual user-chat/message action occurs.
ProjectionCard authority=derived; commercial C remains outside daily operations.
Frozen 36/36, 108/108 and historical OpenAI 7/7 remain unchanged; production HOLD.
