CREATE TABLE IF NOT EXISTS authority (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 0)
);
CREATE TABLE IF NOT EXISTS grants (
    event_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    scope TEXT NOT NULL,
    revoked INTEGER NOT NULL CHECK (revoked IN (0, 1)),
    PRIMARY KEY (event_id, principal, scope)
);
CREATE TABLE IF NOT EXISTS revisions (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    scope TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    body BLOB,
    UNIQUE (event_id, revision),
    CHECK ((status = 'active') = (body IS NOT NULL))
);
