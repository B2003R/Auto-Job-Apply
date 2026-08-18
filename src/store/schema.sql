-- Schema is applied idempotently on every start; add migrations by appending
-- guarded statements rather than editing history.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per discovered job posting. `dedupe_key` is a normalised
-- company+title+source fingerprint so the same role surfacing on several boards
-- is never applied to twice.
CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,
  source         TEXT NOT NULL,
  external_id    TEXT,
  url            TEXT NOT NULL,
  apply_url      TEXT,
  company        TEXT,
  title          TEXT,
  location       TEXT,
  dedupe_key     TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'queued',
  attempt_count  INTEGER NOT NULL DEFAULT 0,
  discovered_at  TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, source);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs (discovered_at);

-- One row per end-to-end application attempt. Retries create new attempts so the
-- history of what was tried, and why it failed, is never overwritten.
CREATE TABLE IF NOT EXISTS attempts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id          TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  mode            TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  final_state     TEXT,
  ats_vendor      TEXT,
  final_url       TEXT,
  failure_class   TEXT,
  failure_message TEXT,
  steps_completed INTEGER NOT NULL DEFAULT 0,
  submitted       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts (job_id);
CREATE INDEX IF NOT EXISTS idx_attempts_started ON attempts (started_at);

-- Append-only state-machine trail. Every transition lands here before the next
-- action runs, which is what makes an interrupted run safely resumable.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT REFERENCES jobs (id) ON DELETE CASCADE,
  attempt_id INTEGER REFERENCES attempts (id) ON DELETE CASCADE,
  ts         TEXT NOT NULL,
  state      TEXT NOT NULL,
  message    TEXT,
  data       TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_attempt ON events (attempt_id, id);

-- ATS accounts the agent created. Passwords are AES-256-GCM ciphertext keyed by
-- VAULT_KEY; the plaintext never touches the database or the logs.
CREATE TABLE IF NOT EXISTS credentials (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  domain          TEXT NOT NULL,
  email           TEXT NOT NULL,
  password_cipher TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  verified_at     TEXT,
  notes           TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_domain_email ON credentials (domain, email);

-- Screenshots and DOM snapshots kept as submission evidence and failure triage.
CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT REFERENCES jobs (id) ON DELETE CASCADE,
  attempt_id INTEGER REFERENCES attempts (id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  path       TEXT NOT NULL,
  ts         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_attempt ON artifacts (attempt_id);

-- Required questions with no match in the answer bank. These are the agent's
-- feedback loop: each row is a prompt to extend config/answers.yaml so the same
-- application is not blocked again tomorrow.
CREATE TABLE IF NOT EXISTS unanswered_questions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT REFERENCES jobs (id) ON DELETE SET NULL,
  domain     TEXT,
  question   TEXT NOT NULL,
  field_type TEXT,
  options    TEXT,
  seen_count INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_unanswered_question ON unanswered_questions (question);

-- Daily submission counters backing the 50/day target and per-source caps.
CREATE TABLE IF NOT EXISTS daily_counters (
  day       TEXT NOT NULL,
  source    TEXT NOT NULL,
  submitted INTEGER NOT NULL DEFAULT 0,
  attempted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, source)
);
