-- Apply once to an empty, disposable database. All objects are namespace-local.
DO $$
BEGIN
    IF current_database() !~ '^gm02as_test_[a-z0-9_]+$' THEN
        RAISE EXCEPTION 'GM-02AS isolation database required';
    END IF;
END $$;

CREATE SCHEMA gm02as;
SET LOCAL search_path = gm02as, pg_catalog;
CREATE DOMAIN sha256_hex AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TABLE authority_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    authority_incarnation bigint NOT NULL CHECK (authority_incarnation > 0),
    schema_version integer NOT NULL CHECK (schema_version = 1)
);
INSERT INTO authority_metadata VALUES (true, 1, 1);

CREATE TABLE principal_heads (
    principal_id text PRIMARY KEY,
    epoch bigint NOT NULL DEFAULT 1 CHECK (epoch > 0),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE identity_links (
    issuer text NOT NULL,
    subject text NOT NULL,
    principal_id text NOT NULL REFERENCES principal_heads,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (issuer, subject)
);

CREATE TABLE login_sessions (
    login_id text PRIMARY KEY,
    principal_id text NOT NULL REFERENCES principal_heads,
    issuer text NOT NULL,
    subject text NOT NULL,
    principal_epoch bigint NOT NULL CHECK (principal_epoch > 0),
    identity_revision bigint NOT NULL CHECK (identity_revision > 0),
    epoch bigint NOT NULL DEFAULT 1 CHECK (epoch > 0),
    active boolean NOT NULL DEFAULT true,
    evidence_state text NOT NULL CHECK (evidence_state IN ('active', 'unknown', 'revoked')),
    observed_at timestamptz NOT NULL,
    evidence_until timestamptz NOT NULL,
    FOREIGN KEY (issuer, subject) REFERENCES identity_links,
    CHECK (evidence_until BETWEEN observed_at AND observed_at + interval '30 seconds'),
    CHECK (evidence_state <> 'revoked' OR NOT active)
);

CREATE TABLE conversation_heads (
    conversation_id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES principal_heads,
    selected_version text NOT NULL,
    authorization_revision bigint NOT NULL DEFAULT 1 CHECK (authorization_revision > 0),
    projection_revision bigint NOT NULL DEFAULT 1 CHECK (projection_revision > 0),
    active boolean NOT NULL DEFAULT true,
    active_window_id text NOT NULL,
    model_run_id text NOT NULL,
    context_epoch bigint NOT NULL CHECK (context_epoch >= 0),
    active_request_id text
);

CREATE TABLE session_versions (
    conversation_id text NOT NULL REFERENCES conversation_heads,
    version_id text NOT NULL,
    owner_id text NOT NULL REFERENCES principal_heads,
    provider text NOT NULL CHECK (provider IN ('api', 'codex')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (conversation_id, version_id)
);
ALTER TABLE conversation_heads ADD FOREIGN KEY (conversation_id, selected_version)
    REFERENCES session_versions DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE conversation_grants (
    conversation_id text NOT NULL REFERENCES conversation_heads,
    principal_id text NOT NULL REFERENCES principal_heads,
    role text NOT NULL CHECK (role IN ('owner', 'delegate')),
    epoch bigint NOT NULL CHECK (epoch > 0),
    active boolean NOT NULL,
    PRIMARY KEY (conversation_id, principal_id)
);

CREATE TABLE g3_dataset_heads (
    dataset_id text PRIMARY KEY,
    generation text NOT NULL,
    generation_revision bigint NOT NULL DEFAULT 1 CHECK (generation_revision > 0),
    authorization_epoch bigint NOT NULL DEFAULT 1 CHECK (authorization_epoch > 0),
    cursor_upper bigint NOT NULL DEFAULT 0 CHECK (cursor_upper >= 0),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE g3_source_heads (
    dataset_id text NOT NULL REFERENCES g3_dataset_heads,
    source_id text NOT NULL,
    event_id text NOT NULL,
    generation_revision bigint NOT NULL CHECK (generation_revision > 0),
    revision bigint NOT NULL CHECK (revision > 0),
    cursor_position bigint NOT NULL CHECK (cursor_position > 0),
    content_sha256 sha256_hex NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (dataset_id, source_id)
);

-- Activation needs both a dataset grant (source_id NULL) and each source grant.
CREATE TABLE g3_grants (
    dataset_id text NOT NULL REFERENCES g3_dataset_heads,
    source_id text,
    principal_id text NOT NULL REFERENCES principal_heads,
    epoch bigint NOT NULL CHECK (epoch > 0),
    active boolean NOT NULL,
    UNIQUE NULLS NOT DISTINCT (dataset_id, source_id, principal_id),
    FOREIGN KEY (dataset_id, source_id) REFERENCES g3_source_heads
);

CREATE TABLE consumption_bindings (
    receipt_id sha256_hex PRIMARY KEY,
    authority_incarnation bigint NOT NULL CHECK (authority_incarnation > 0),
    conversation_id text NOT NULL,
    version_id text NOT NULL,
    attempt_request_id text NOT NULL,
    packet_sha256 sha256_hex NOT NULL,
    input_sha256 sha256_hex NOT NULL,
    sealed_binding jsonb NOT NULL,
    UNIQUE (authority_incarnation, conversation_id, attempt_request_id),
    FOREIGN KEY (conversation_id, version_id) REFERENCES session_versions
);

CREATE TABLE activation_requests (
    activation_request_id text PRIMARY KEY,
    authority_incarnation bigint NOT NULL CHECK (authority_incarnation > 0),
    request_sha256 sha256_hex NOT NULL,
    principal_id text NOT NULL REFERENCES principal_heads,
    conversation_id text NOT NULL REFERENCES conversation_heads,
    receipt_id sha256_hex NOT NULL REFERENCES consumption_bindings DEFERRABLE INITIALLY DEFERRED,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (split_part(activation_request_id, '.', 1) = authority_incarnation::text)
);

CREATE TABLE activation_decisions (
    activation_request_id text PRIMARY KEY REFERENCES activation_requests,
    receipt_id sha256_hex NOT NULL REFERENCES consumption_bindings,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    evidence_deadline timestamptz,
    outcome text NOT NULL CHECK (outcome IN ('activated', 'denied')),
    reason text,
    projection_revision bigint,
    authority_incarnation bigint NOT NULL,
    conversation_id text NOT NULL REFERENCES conversation_heads,
    from_window_id text NOT NULL,
    from_model_run_id text NOT NULL,
    target_window_id text NOT NULL,
    model_run_id text NOT NULL,
    CHECK ((outcome = 'activated' AND reason IS NULL AND evidence_deadline IS NOT NULL
            AND projection_revision IS NOT NULL AND projection_revision > 0)
        OR (outcome = 'denied' AND reason IS NOT NULL AND evidence_deadline IS NULL AND projection_revision IS NULL))
);
CREATE UNIQUE INDEX one_activation_per_receipt ON activation_decisions (receipt_id)
    WHERE outcome = 'activated';
CREATE INDEX activated_runtime_history ON activation_decisions (authority_incarnation, conversation_id)
    WHERE outcome = 'activated';

CREATE TABLE authority_commands (
    command_id text PRIMARY KEY,
    authority_incarnation bigint NOT NULL CHECK (authority_incarnation > 0),
    command_sha256 sha256_hex NOT NULL,
    result jsonb,
    CHECK (split_part(command_id, '.', 1) = authority_incarnation::text)
);

CREATE TABLE revocation_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority_incarnation bigint NOT NULL CHECK (authority_incarnation > 0),
    command_id text NOT NULL UNIQUE,
    scope text NOT NULL,
    subject_keys jsonb NOT NULL,
    new_epoch bigint NOT NULL CHECK (new_epoch > 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- audit_id identifies a row. It does not establish cross-scope commit order.

CREATE FUNCTION gm02as.reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'GM-02AS append-only record' USING ERRCODE = '55000';
END $$;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON consumption_bindings
    FOR EACH ROW EXECUTE FUNCTION gm02as.reject_history_mutation();
CREATE TRIGGER immutable_request BEFORE UPDATE OR DELETE ON activation_requests
    FOR EACH ROW EXECUTE FUNCTION gm02as.reject_history_mutation();
CREATE TRIGGER immutable_decision BEFORE UPDATE OR DELETE ON activation_decisions
    FOR EACH ROW EXECUTE FUNCTION gm02as.reject_history_mutation();
CREATE TRIGGER immutable_revocation BEFORE UPDATE OR DELETE ON revocation_audit
    FOR EACH ROW EXECUTE FUNCTION gm02as.reject_history_mutation();

-- The final pre-commit check covers time spent between the decision and COMMIT.
-- Login and all authorization heads remain locked by the activation transaction.
CREATE FUNCTION gm02as.check_activation_deadline() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.outcome = 'activated' AND clock_timestamp() >= NEW.evidence_deadline THEN
        RAISE EXCEPTION 'GM-02AS evidence expired before commit' USING ERRCODE = 'P7201';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER activation_evidence_deadline AFTER INSERT ON activation_decisions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gm02as.check_activation_deadline();
