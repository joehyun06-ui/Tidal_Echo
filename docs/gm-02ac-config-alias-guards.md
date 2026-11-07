# GM-02AC: current-source captured config aliases obey explicit binding

The seven original API-loop config functions now check the server-only module
binding at entry. With no binding they keep their existing behavior. No startup
invocation, environment activation or request-controlled flag is added. When the
existing pre-startup installer is explicitly used, it publishes entry callbacks
alongside the rebound module globals and rejects an existing callback binding.

Original load_config/history_n references captured from this source version
before installation now enter the coordinated read callbacks too. Their guard
runs before original broad exception/fallback handling, so pending/ACL failures
are not swallowed into empty config or default count. Original save_config,
create_session and save_sessions references refuse before their original body;
update_config/patch_session references require the exact bound command and
resource checks, then retain the original coordinated branch. No request ID is
allocated or original write silently rerouted. Original callbacks delegate to
existing buffered legacy commands, without recursive reentry or duplicate effects.

Unit tests capture the actual seven original function objects before installation
in an isolated module compiled from the real source, test all direct write
refusals, current read permission/pending behavior, once-only coordinated execution
and default uninstalled behavior. A full API-loop test process separately captures
its original aliases before binding, probes both reads and all five write entry
guards, then starts real Uvicorn/TCP; candidate original-reference execution still
commits once. The probe is CLI-only test setup, not an HTTP debug endpoint.

This extends AB's selected-function binding, not a universal code sandbox. It
cannot retrofit guard code into function objects from older pre-upgrade bytecode,
serialized closures, or old already-running processes. Direct Path/SQLite/manual
IO, other functions/modules/processes, trusted rebinding, native/CLI traffic and
initial import-time validation remain outside. Installation still requires an
unwrapped module before traffic and fixed command/coordinator/lock/resources;
no hot-install/uninstall, cancellation or production bootstrap is introduced.
Whole higher-level multi-read transactions, unrelated exception swallowing,
runtime/model/window consumption, client recovery UI, TLS/proxy and production
backup/recovery remain open. Existing defaults, flags, frozen baselines, G3 truth/
derived cards, commercial C independence and production HOLD remain unchanged.
