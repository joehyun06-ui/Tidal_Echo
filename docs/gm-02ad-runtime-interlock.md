# GM-02AD: restricted API runtime-entry admission

After config IO binding and before startup, an explicit server call may install
the default-off API-loop runtime interlock. It blocks new entry into handle_ingest,
run_model, frozen provider-contract execution, provider dispatch/stream/completion
functions, provider client construction and relay_out. Current-source captured
function aliases also check the module guard; unstarted coroutines check when
awaited. Decorated provider entry points have an outer guard before existing
usage capture so rejection does not first create observation side effects.

Restricted mode always refuses with 423 lifecycle_runtime_unavailable. It is
not a model-execution grant, window adapter, automatic switch or normal chatting
mode. Metadata prepare/execute/status remains usable. No guard is installed by
default app startup or environment; uninstalled factory behavior remains as
before. Installer requires the config binding, unwrapped module function globals,
prestartup app state and no previous runtime guard.

Tests capture all eleven original entry points, reject provider/body/client/sink/
callback effects, check default mocked client construction, coroutine timing and
installation guards. Full API-loop/Uvicorn TCP tests install config IO plus this
runtime guard without HTTP interlock, probe generation/completion/relay/client
entry refusal and send an actual ingest request. It returns 423 with unchanged
messages/config/generation, while a coordinated metadata request commits once.
The test server uses synthetic data and loopback-only network guards.

This only prevents new entry at these selected current-source functions. It
does not cancel, recover or make safe already-dispatched operations, streaming
callbacks already in progress, old bytecode/processes, reflection/unwrapped
decorator internals, direct HTTPX/native/CLI/provider-control calls or external
workers. Trusted server code can replace globals. It is not an all-runtime or
capability boundary and does not prove provider receipt, source consumption or
chat/window continuity. Actual coordinated runtime execution, all ingress paths,
native/Codex, TLS/proxy, client recovery UI and production backup/recovery remain
open. Production flags/defaults/CI/dependencies are unchanged. G3 authority,
derived cards, frozen baselines and commercial C independence remain; production HOLD.
