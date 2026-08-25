-- Every scheduled run leaves a row, whether or not it had anything to say.
--
-- This project's recurring failure is not a crash: it is a job that runs,
-- decides nothing, sends nothing, and exits 0. Six of seven scheduled jobs
-- were doing exactly that for weeks, and the only evidence was an absence of
-- email - which is indistinguishable from a quiet week.
--
-- `recommendations` only records findings, so it cannot tell "ran and found
-- nothing" from "did not run". This can.

CREATE TABLE IF NOT EXISTS job_runs (
    id          {SERIAL_PK},
    job         TEXT NOT NULL,
    season      INTEGER,
    week        INTEGER,
    -- ok | nothing_to_do | skipped | failed
    status      TEXT NOT NULL,
    -- What it looked at and what it decided, for the weekly digest.
    detail      TEXT,
    exit_code   INTEGER,
    duration_ms INTEGER,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_runs_recent ON job_runs(job, finished_at);
