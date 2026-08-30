# P3-B provider-guard deployment note

This phase adds a provider-aware API-loop guard and a P3 supervisor without enabling Codex.

Production activation is intentionally separate: keep `CODEX_CONTROL_ENABLED=false`, `CODEX_CANARY_ENTRYPOINTS_ENABLED=false`, and `CODEX_GENERATION_ENABLED=false`; then select `python scripts/render_start_p3.py` only after exact-head CI is green.

The P3 supervisor preserves the existing relay and autonomous-wake commands. With Codex entrypoints disabled it changes only the localhost API-loop target from `examples.api_loop:app` to `examples.api_loop_provider_guard:app`.

The guard permanently respects durable Web-session provider authority. An API-authority or unknown session delegates to the existing API loop. An explicit or historical Codex-authority Web session returns `codex_generation_disabled` before any API model call. It cannot create a Codex session or start a Codex runtime.
