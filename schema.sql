-- SQLite schema for the U.S. outage monitor MVP. All timestamps are ISO-8601 UTC strings;
-- the dashboard converts them to America/New_York for display.
PRAGMA foreign_keys = ON;

-- Health and freshness of each configured feed (feeds come only from sources.json).
CREATE TABLE IF NOT EXISTS source_status (
    source_key      TEXT PRIMARY KEY,
    last_attempt    TEXT,
    last_success    TEXT,
    last_error      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    etag            TEXT,
    last_modified   TEXT,
    retry_after     TEXT
);

-- Every feed entry as fetched. Deduplicated on (source_key, item_key).
CREATE TABLE IF NOT EXISTS source_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT NOT NULL,          -- poller feed key, or 'analyst' for research citations
    item_key        TEXT NOT NULL,          -- feed guid, else link, else hash
    tier            TEXT NOT NULL,          -- official | agency | news | forum | threat_intel | tracker
    publisher       TEXT NOT NULL,
    title           TEXT NOT NULL,          -- plain text (HTML stripped)
    summary         TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL,
    published_at    TEXT,                   -- time the source says it published/updated the item
    fetched_at      TEXT,                   -- time this system fetched it (NULL for analyst entries)
    content_hash    TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    candidate_id    INTEGER REFERENCES candidates(id),
    UNIQUE (source_key, item_key)
);

-- Normalized incident candidates. Everything starts as 'unverified'.
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key     TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    organization    TEXT NOT NULL,
    service         TEXT NOT NULL DEFAULT 'unspecified',
    sector          TEXT NOT NULL DEFAULT 'unknown',
    evidence_class  TEXT NOT NULL DEFAULT 'unverified',  -- unverified | attributed | confirmed | dismissed
    severity        TEXT NOT NULL DEFAULT 'unknown',     -- unknown | low | moderate | high | critical
    scope           TEXT NOT NULL DEFAULT 'unknown',     -- unknown | national | multi_state | state | local
    scope_basis     TEXT NOT NULL DEFAULT 'unknown',     -- unknown | reported | confirmed
    scope_detail    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'unknown',     -- unknown | ongoing | resolved | monitoring
    event_start     TEXT, event_start_approx INTEGER NOT NULL DEFAULT 0,
    event_end       TEXT, event_end_approx   INTEGER NOT NULL DEFAULT 0,
    first_reported  TEXT,
    last_source_update TEXT,
    inclusion_reason TEXT NOT NULL DEFAULT '',
    impact          TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL DEFAULT 'poller',      -- poller | analyst_research
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Chronological events for a candidate (event time vs. report time kept separate).
CREATE TABLE IF NOT EXISTS timeline (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    event_at        TEXT NOT NULL,
    approximate     INTEGER NOT NULL DEFAULT 0,
    text            TEXT NOT NULL,
    evidence        TEXT NOT NULL,          -- confirmed | attributed | unverified
    source_item_id  INTEGER REFERENCES source_items(id)
);

-- Checkable claims made about a candidate and how they hold up.
CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    claim           TEXT NOT NULL,
    claimant        TEXT NOT NULL,
    claim_date      TEXT,
    verdict         TEXT NOT NULL,          -- supported | unsupported | contradicted | misleading | corrected | unresolved
    rationale       TEXT NOT NULL,
    source_item_id  INTEGER REFERENCES source_items(id)
);

-- Alerts raised by the poller or by review changes; repeats increment occurrences.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    kind            TEXT NOT NULL,          -- new_primary_signal | evidence_changed | severity_changed | source_updated
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    UNIQUE (candidate_id, kind, message)
);

-- Append-only record of every operator or analyst decision.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER REFERENCES candidates(id),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    before_json     TEXT,
    after_json      TEXT,
    note            TEXT NOT NULL,
    at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_candidate ON source_items(candidate_id);
CREATE INDEX IF NOT EXISTS idx_items_published ON source_items(published_at);
CREATE INDEX IF NOT EXISTS idx_cand_class ON candidates(evidence_class);
