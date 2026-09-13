-- The sweep's own record of what it actually covered.
--
-- reconcile.py deactivates listings on ABSENCE: everything on a portal that a
-- complete sweep did not see is offline. That is only safe if "complete" is a
-- fact rather than an assumption, and an exit code is not enough -- a sweep
-- blocked at page 48 of 172 still exits 0. So each run records what it set out
-- to cover and whether it got all the way there.
--
-- Applied to production 2026-09-13.
CREATE TABLE IF NOT EXISTS fixnflip_v2.sweep_run (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source        text        NOT NULL,
    categories    text[]      NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    complete      boolean     NOT NULL DEFAULT FALSE,
    pages_ok      integer     NOT NULL DEFAULT 0,
    pages_failed  integer     NOT NULL DEFAULT 0,
    detail        text
);

CREATE INDEX IF NOT EXISTS sweep_run_source_complete_idx
    ON fixnflip_v2.sweep_run (source, complete, started_at DESC);

COMMENT ON TABLE fixnflip_v2.sweep_run IS
 'One row per sweep (find --sweep). complete = every page of every listed category fetched successfully and the last page reached; reconcile.py deactivates on absence only against complete runs.';
