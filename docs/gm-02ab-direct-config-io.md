# GM-02AB: selected API-loop config globals under explicit pre-startup binding

install_api_loop_config_io(loop, adapter, access=server_access, enabled=False)
is a server-only opt-in. Off accesses no dependencies or globals. On requires a
plain unwrapped module, current loop.update grant, matching resolved config/DB,
same concrete adapter/coordinator resource, no previous binding and no started
FastAPI middleware stack. Validation precedes assignment; installation must not
race live work. Command/coordinator/lock identities and resource paths stay bound;
retargeted globals refuse rather than silently reading another/older binding.

| Selected module global after explicit binding | Behavior |
|---|---|
| load_config | Current server grant, shared session lock and observe_config; private independent config copy |
| history_n | Strict coordinated count projection; read failure raises, never swallowed into default count |
| save_config | Always refuses direct persistence |
| update_config / patch_session | Only explicit coordinated keyword with the exact bound command instance may delegate |
| create_session / save_sessions | Refused in this restricted binding, before original function/allocation |

The full config copy stays inside trusted server code, not a new HTTP response.
Existing MVP validation is retained. Current auth remains original HTTP auth plus
the configured server principal/current command ACL for internal reads; no new
read-only role, request-made principal or credential is invented. Packet storage
ACL is not used for config reads that do not access packets. Repeated reads in
one high-level function are independent snapshots, not a whole-call transaction.

Existing plain-module functions dynamically resolve globals, so ordinary module
calls and selected legacy HTTP handlers use these bindings. Commands operate on
their existing buffered legacy objects and do not recurse into module save_config.
No request ID is allocated, no mutation is silently rerouted, and no provider/
model request or automatic recovery occurs. A pending intent blocks old config/
session reads until explicit original recovery. count/config errors become fixed
HTTP exceptions; default count is not presented as verified data.

Unit tests compile the exact seven existing API-loop functions into an isolated
plain module and exercise binding/defaults/resource/permission/read/command rules.
Full TCP tests import the complete application, install this binding without the
HTTP interlock, and prove actual old config/create/patch handlers refuse while
coordinated history writes and guarded reads work. A publication failure plus
real server restart retains pending; old read handlers continue to refuse until
original-reference recovery. Test workers remain loopback-only with synthetic
data and model/native control disabled.

This is not a filesystem capability boundary, universal background-task fence,
native integration or all-writer/read/ingress/runtime guarantee. Old function
aliases captured before binding, direct Path/SQLite/manual IO, other modules or
processes and trusted code that replaces globals can bypass it. Initial module
import-time config validation also precedes this binding. Existing canary/
wrapped modules are deliberately rejected rather than assumed compatible. No
runtime uninstall/hot-install is supported, and it does not cancel in-flight work.
Higher-level exception swallowing, unrelated getters and complete model/window
consumption still need separate integration review. Normal legacy chat/client
compatibility, startup provisioning, TLS/proxy, client recovery UI and production
backup/recovery are open. Default app startup/flags/dependencies/CI remain unchanged.
Prior private-FS/fsync/size/orphan-stage, named-path not inode/antirollback,
SQL/JSON and external ACL limits remain; G3 truth/derived cards, frozen baselines,
commercial C independence and production HOLD are unchanged.
