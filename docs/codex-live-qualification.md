# Codex Web canary live qualification

This runbook qualifies the staged Codex Web canary without changing the provider authority of normal Web sessions, Telegram, autonomous wake, Kelivo, Memory formation, or Operit.

The companion CLI is `scripts/codex_live_qualification.py`. Its default `plan` command performs **no network I/O**. The CLI never edits Render configuration, changes a start command, triggers a deploy/restart, or sends a chat message on the user's behalf.

## Preconditions

Do not begin live qualification until the stacked P2 PRs have been independently reviewed/merged in order and their production deployment state is known:

1. P2-A generation protocol/shared transport foundation.
2. P2-B durable generation store/worker/canary controller.
3. P2-C alternate canary relay/api-loop entrypoints.
4. P2-D alternate default-off supervisor.
5. P2-E authenticated canary admin proxy.

Before every live state transition, independently verify the current Render deploy, branch, and environment. A green GitHub CI run is not evidence that production has deployed or enabled the feature.

Set operator-local variables without putting the relay secret on argv:

```bash
export CODEX_QUALIFICATION_BASE_URL='https://<relay-host-or-prefix>'
export RELAY_SECRET='<existing relay secret>'
python scripts/codex_live_qualification.py plan
```

The CLI accepts HTTPS endpoints. Plain HTTP is accepted only for localhost/loopback testing.

## Phase 1 — stage alternate entrypoints, generation still off

**Render/operator action, not performed by the harness:**

- select the alternate canary supervisor;
- set `CODEX_CANARY_ENTRYPOINTS_ENABLED=true`;
- keep `CODEX_GENERATION_ENABLED=false`;
- enable `CODEX_CONTROL_ENABLED=true` for P1 qualification;
- restart/deploy explicitly and wait for the existing service readiness gates.

Then run:

```bash
python scripts/codex_live_qualification.py status
```

Required result:

- provider status is reachable;
- `generation_provider` is still exactly `api`;
- `connected` is a boolean.

If disconnected, start the device-code flow:

```bash
python scripts/codex_live_qualification.py login-start
```

Open the returned HTTPS verification URL yourself and enter the returned user code. Do not automate the browser login in this qualification.

After completing the browser flow:

```bash
python scripts/codex_live_qualification.py account-check
```

Required result:

- `connected=true`;
- `generation_provider=api`;
- usage endpoint is available.

At this point normal generation is still API. No Codex thread/turn should have been created by the harness.

## Phase 2 — enable the Codex canary worker

**Render/operator action, not performed by the harness:**

- keep alternate canary entrypoints enabled;
- set `CODEX_GENERATION_ENABLED=true`;
- restart the service explicitly;
- do not alter Telegram/Kelivo/Memory/Operit/wake provider settings.

Immediately re-run:

```bash
python scripts/codex_live_qualification.py account-check
```

This is the first persistence gate: ChatGPT authentication must survive the restart through the persistent Codex home, and provider authority must still report API for the normal surface.

## Phase 3 — create one explicit canary window

Create exactly one canary session:

```bash
python scripts/codex_live_qualification.py canary-create --title 'Codex canary'
```

Record the returned `api_session`. Refresh/reopen GuiTing so the new server-side session appears, then manually select that session.

Before sending a message, check it is active and has no thread yet:

```bash
python scripts/codex_live_qualification.py canary-status --session '<api_session>'
```

Expected before first turn:

- `status=active`;
- `thread_bound=false`;
- `model_provider` may still be `unresolved`.

## Phase 4 — first pure-text turn

In GuiTing, manually send **one ordinary text-only message** in the canary window.

Do not use:

- attachments/images;
- voice;
- a message that relies on transient Web↔Telegram continuity;
- another window/session;
- simultaneous second input before the first reply completes.

Visually confirm **exactly one** assistant reply is persisted in the same canary window. The harness intentionally does not send this message and does not claim that `thread_bound=true` alone proves a completed visible reply.

Then run:

```bash
python scripts/codex_live_qualification.py wait-bound --session '<api_session>'
```

Required result:

- `thread_bound=true`;
- `model_provider` is no longer `unresolved`.

After the visible reply has been confirmed, save a restart receipt:

```bash
python scripts/codex_live_qualification.py snapshot \
  --session '<api_session>' \
  --receipt './codex-canary-receipt.json'
```

The receipt contains only the sanitized API-authority/account boolean and the canary's session/model/provider/reasoning/thread-bound contract. It does **not** contain the relay secret, ChatGPT account identity, thread id, cwd, prompt/chat text, persona hash, callback identity, client message id, or input digest. On POSIX the file is created with mode `0600`.

## Phase 5 — restart persistence

**Render/operator action, not performed by the harness:** restart/redeploy the service **without changing the canary/control/generation gates**.

Then run:

```bash
python scripts/codex_live_qualification.py verify-after-restart \
  --receipt './codex-canary-receipt.json'
```

Required result:

- ChatGPT account is still connected;
- normal provider authority is still API;
- the same canary session remains active;
- thread remains bound;
- model, model provider, and reasoning effort exactly match the pre-restart receipt.

Now manually send one second text-only message in the same canary window and visually confirm exactly one reply. This validates resume behavior after process restart; the harness deliberately does not synthesize this chat traffic.

## Phase 6 — retire the canary

Retire the canary only when there is no active generation:

```bash
python scripts/codex_live_qualification.py canary-retire --session '<api_session>'
python scripts/codex_live_qualification.py verify-retired --session '<api_session>'
```

Retirement is one-way for that canary session. Do not repin a retired session to its old Codex thread after API traffic has diverged.

## Phase 7 — rollback

**Render/operator action, not performed by the harness:** perform the intended rollback explicitly. A full rollback normally means generation off, canary entrypoints off/legacy supervisor restored, and P1 control off unless there is a deliberate reason to keep it enabled.

After the service is healthy:

```bash
python scripts/codex_live_qualification.py rollback-check --expect-control-disabled
```

Required result:

- `/healthz` succeeds;
- `/readyz` succeeds;
- provider control is explicitly disabled.

If control is intentionally kept enabled during a staged rollback, omit `--expect-control-disabled`; the harness still requires `generation_provider=api`.

## Fail-closed rules

Stop qualification and do **not** auto-fallback to API for the canary session when any of the following occurs:

- generation dispatch is uncertain;
- restart reconciliation cannot prove the persisted turn state;
- canary status/provider/model contract changes unexpectedly;
- transient continuity is applied or unavailable for a pinned canary input;
- canonical message text/source/session does not match canary admission;
- an attachment or unsupported input reaches the canary;
- callback correlation fails;
- the worker reports a terminal failure/interruption.

A canary failure is not permission to send the same input through the API provider. Resolve or retire the canary first.

## Qualification is not cutover

Passing this runbook qualifies only the explicit text-only Web canary. It does not qualify automatic Web-session migration, existing-session history migration, transient-continuity equivalence, native multimodal Codex input, Telegram, autonomous wake, Kelivo, Memory formation, or Operit. Those surfaces retain their independent migration/rollback gates.
