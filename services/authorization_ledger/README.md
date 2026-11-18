# GM-02AS isolated PostgreSQL authorization ledger

This package is an acceptance implementation on top of Draft #246. The ledger
has no Relay startup or HTTP wiring. It runs with a disposable local PostgreSQL
17+ cluster, synthetic IdP and G3 facts, and a synthetic SQLite projection.
Production readiness is false. All five production gates remain blocked.

## Run the acceptance suite

From the repository root, using a non-root local account and PostgreSQL server
binaries (`initdb`, `pg_ctl`, `postgres`):

```bash
python -m pip install -r services/authorization_ledger/requirements-test.txt
GM02AS_TEST_PG_BIN=/path/to/postgresql/bin \
  python -m unittest services.authorization_ledger.acceptance -v
```

The runner creates a private cluster and fixture-only databases, listens on an
ephemeral port on 127.0.0.1, uses a per-run SCRAM password and HMAC key, and
removes the databases when it exits. The ledger has no DSN discovery. Its
connection factory must supply a local connection to a `gm02as_test_*`
database. The SQL migration checks this database name before writing.

## Transaction API

- `install_schema` applies the namespace-local migration once in an empty test
  database. Identity, owner, initial login, version and G3 rows are seeded by
  the acceptance fixture.
- `issue_credential` resolves the already authenticated host scope and produces
  a sealed fence for one dataset and a sorted set of sources. `validate_scan`
  rechecks the scope, source hashes, revisions, generation revision, ACL epochs
  and committed cursor upper bound. `seal_package` binds the expected runtime
  observation to that fence. `record_consumption` requires the observed v2
  consumed-input hash, exact window/run, and source/message cursors.
- `register_activation` stores a pending command under a globally unique
  incarnation-prefixed ID. The server computes its digest from the request ID,
  sealed package and receipt claim. Re-registering identical input is
  idempotent. A different digest conflicts and cross-principal lookups expose
  no other principal's result. A consumption binding can authorize one
  successful activation.
- `activate` commits the decision and PostgreSQL conversation/window state
  together. It rechecks the complete fence and evidence freshness after taking
  the relevant locks. A deferred constraint checks the evidence deadline at
  COMMIT. `OutcomeUnknown` carries the original ID. Call `query_activation`
  using that ID and the independently authenticated principal. The query never
  executes an activation. A pending decision may be submitted again using the
  original ID and digest. An absent registration may be retried with that same
  ID and digest. A recorded decision is returned with a fresh
  `effective_now` calculation. A denied decision remains denied.
- `set_principal_access`, `observe_idp`, `set_conversation_grant`,
  `select_version`, `set_g3_grant`, `publish_revision` and
  `publish_generation` serialize their mutations on the relevant heads and
  record a digest-bound command result. Revocation advances epochs and appends
  an audit row in the same transaction. G3 publishes revision and cursor
  together, and uses an expected numeric generation revision to reject late
  writes even if a generation label is reused. Source bodies are outside the
  ledger. The trusted fixture publisher supplies a digest for an already
  durable body.

Transactions use PostgreSQL READ COMMITTED, explicit locks and synchronous
commit. The order is authority metadata, principal, login, conversation,
dataset, sorted sources, request, receipt. Normal operations hold a shared
incarnation lock. Recovery holds it exclusively. Control commands acquire
their idempotency records after the affected heads. The host's test IdP
observation has a 30-second maximum freshness budget. This is a fixture
policy, with no measured production provider latency.

## Recovery, projection and #246

The simulated PITR uses a real PostgreSQL database image copied before
revocation, and a separate durable file controller that stays outside the
restored state. The controller fences writers during recovery. Reconciliation
retires restored effective grants, identities and sessions under a new
incarnation while preserving historical rows. This conservative test policy
demonstrates anti-resurrection and requires fresh provisioning. Production
WAL replay, immutable external watermark reconciliation and independently
authorized control-plane operators remain outside this implementation.

PostgreSQL emits detached display snapshots. The test SQLite consumer applies
an explicit incarnation and monotonically increasing per-conversation
revision, ignoring old and duplicate messages. It never participates in an
authorization transaction.

The minimum adapter changes for #246 are:

1. Keep the SessionLifecycle preparation, canonical message cursor and runtime
   observation checks. Pass their selected-version and target runtime binding
   into the authoritative PostgreSQL activation command. The committed
   PostgreSQL decision provides the result that Relay projects.
2. Keep the v2 consumption receipt and input digest. Associate the trusted
   observation with the ledger's sealed incarnation, login/grant epochs and
   G3 source fence, and persist the ledger receipt binding before activation.
   The current code consumes the existing `ApiWindowConsumptionObservation`
   type using synthetic observations. It does not install a new receipt route.
3. Carry the ledger's global activation ID and canonical digest through the
   gateway. Expose the committed historical decision separately from
   `effective_now`. On an unknown response, query the original ID. This
   package has no external sender and does not alter #246 call sites.

The isolated API trusts its host for caller identity, command authorization,
runtime observations and publication. It is deliberately unwired from
production IdP, G3, the Relay database and any public service.
