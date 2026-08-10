-- Cold-email job agent datastore. See PROJECT.md §6 for the design rationale.

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    domain      TEXT,
    source      TEXT NOT NULL,              -- yc | producthunt | startup_india | manual_csv
    raw_tags    TEXT,                       -- JSON array of tags/topics as scraped from the source
    niche       TEXT,                       -- consumer | fintech | data_saas | ai_devtools | NULL (unclassified)
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'classified', 'skipped')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

CREATE TABLE IF NOT EXISTS contacts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES companies(id),
    name                    TEXT,
    role                    TEXT,                       -- raw title, e.g. "Talent Acquisition Lead"
    role_category           TEXT CHECK (role_category IN ('hr', 'product', 'other')),
    email                   TEXT,
    verification_confidence REAL,                       -- 0.0-1.0, from the finder API
    source_api              TEXT,                        -- hunter | apollo | manual
    status                  TEXT NOT NULL DEFAULT 'new' CHECK (
                                status IN ('new', 'verified', 'needs_manual_check', 'rejected')
                            ),
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS email_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id       INTEGER NOT NULL REFERENCES contacts(id),
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    niche            TEXT,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('initial', 'followup')),
    attached_resume  INTEGER NOT NULL DEFAULT 1,        -- 0/1 boolean
    status           TEXT NOT NULL DEFAULT 'pending_review' CHECK (
                        status IN (
                            'pending_review', 'approved', 'rejected',
                            'sent', 'bounced', 'replied', 'no_response'
                        )
                     ),
    gmail_thread_id  TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_queue_contact ON email_queue(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_queue_status ON email_queue(status);

CREATE TABLE IF NOT EXISTS send_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL UNIQUE,       -- YYYY-MM-DD
    sent_count   INTEGER NOT NULL DEFAULT 0,
    bounce_count INTEGER NOT NULL DEFAULT 0
);

-- Single-row config table driving the send ramp (§4.6) and circuit breaker.
CREATE TABLE IF NOT EXISTS send_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    daily_cap       INTEGER NOT NULL DEFAULT 10,
    ramp_step       INTEGER NOT NULL DEFAULT 2,
    ramp_ceiling    INTEGER NOT NULL DEFAULT 25,
    ramp_interval_days INTEGER NOT NULL DEFAULT 7,
    last_ramp_date  TEXT
);
