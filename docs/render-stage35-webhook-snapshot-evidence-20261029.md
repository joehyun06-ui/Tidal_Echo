# Stage 35 — Telegram / persistent-disk recovery evidence, OFFLINE

This independent Draft is based on live `33f2fb9e` and **does not** merge or deploy #170, #171 or #172. No Bot API call, webhook mutation, memory write, paid API, disk restore or Render configuration mutation was authorized or performed.

## Current production read-only observations

- Render still reports `ouou-home-telegram` live at `33f2fb9e` with actual P3 supervisor startup (`python scripts/render_start_p3.py`), `autoDeployTrigger=off`, `/healthz` and 1 GB disk `/var/data`. `dep-dbdd6ld9fdbs73anfue0` at `a2d8699a` is `deactivated`.
- **Blueprint drift:** the merged `render.yaml` still says `python scripts/render_start.py`; Draft #170 already contains a tested proposal to align to P3, but remains unmerged. Do not duplicate or claim it is live.
- Reattempts of both public `/healthz` and `/readyz` from the audit container returned `curl (6)` / HTTP `000` (DNS resolution failed); public page retrieval also could not access the paths. There is **no observed live HTTP status code or body**. Do not equate platform live with readiness.
- 2026-10-28 14:53 UTC had one aggregated 502. At 14:51 UTC one instance-count point was zero during startup, then the series was one from 14:53. Filtered request/error logs lacked a matching row; no causal explanation has been established.

## Safe offline classifiers

`backend/render_telegram_disk_evidence_offline.py` contains only pure metadata functions. Its inputs are explicitly *operator-supplied*. It cannot access Telegram, Render, GitHub, networks, a credential store, a disk, or a database, and it does not write files. Its `EvidenceStatus` only exposes a fixed category, a source name and hardcoded false permission booleans.

- `review_telegram_webhook(raw_getWebhookInfo_dict)` inspects only the locally supplied Bot API result. The fixed expected webhook URL is `https://ouou-home-telegram.onrender.com/integrations/telegram/webhook` as implemented in the deployed app. The submitted URL and `last_error_message` **must never be logged or placed in PR / CI evidence**. A match means claimed registration only; `pending_update_count > 0` does not prove failure, and a historical `last_error_date` does not prove an ongoing failure. No end-to-end Telegram delivery is certified.
- `review_disk_snapshot(raw_snapshot_metadata, queried_disk_id=known_disk_id, disk_mount=verified_mount, observed_at=timezone_aware_datetime)` requires the pinned disk ID from the **Render API request path** and a separately verified mount path. Per Render's published `GET /v1/disks/{diskId}/snapshots` contract, each snapshot element contains `createdAt`, `snapshotKey`, and `instanceId`—**not** its own `diskId` or `mountPath`. Never log or persist a `snapshotKey`; the official API says keys expire after 24 hours and should be re-queried before an authorized restore. Under 48 hours yields only `snapshot_record_observed_restore_unverified`, **not** backup acceptance or restore permission. Do not call the POST restore endpoint as an audit.
- `review_old_deploy(raw_get_deploy_dict)` confirms only that the old known deployment is deactivated. It cannot prove the build artifact remains available for rollback. Git checkout, code hash equality, and metadata are not disk schema compatibility evidence.

Operators should obtain Bot API `getWebhookInfo` in their own controlled environment, reading the Bot Token through a safe secret mechanism, not in shell history, a PR, GitHub Actions, logs or this conversation; do **not** use the existing `configure_telegram_webhook.py` for inspection because it invokes mutating `setWebhook`. Process the raw JSON in memory only, and discard it immediately after generating the fixed summary category. Telegram webhook verification needs the actual authenticated source, not a manually invented example object.

Snapshot availability must be checked in Render's authenticated dashboard/API using read-only metadata and independently confirmed latest timestamp. Review a tested restore path separately; **code rollback will not restore `/var/data`**. When working with SQLite (possibly WAL), use an approved database-consistent backup and verify schema and recovery against a copy, never execute schema migrations or integrity operations on the live disk as part of this audit.

Before any rollback: prove incident scope and source; confirm previous build artifact retention; confirm a restorable disk backup and target schema compatibility; obtain explicit authorization; then perform a separately controlled rollback with post-rollback checks. The offline labels provide **no authorization** for rollback/deploy, OIDC, Telegram mutation, user-memory writes, paid APIs, or v48 gate changes. The official v48 36/36 and 108/108 gates remain frozen.

## Validation

`python -m unittest backend.tests._render_telegram_disk_evidence_offline_worker -v` runs 24 simulated cases with no network. The Windows/Linux CI is constrained to this test and does not hold credentials or enable the live probe. The classifier is **not** connected to the production web server, and no endpoint is added to it.

References: https://api-docs.render.com/reference/list-snapshots and https://api-docs.render.com/reference/restore-snapshot .
