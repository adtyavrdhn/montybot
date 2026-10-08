-- A run the user stopped from the web app.
ALTER TABLE sammy.runs DROP CONSTRAINT runs_status_check;
ALTER TABLE sammy.runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed', 'stopped'));
-- The chat list shows each thread's unfinished run (sammy.store.active_runs).
CREATE INDEX runs_active_by_user ON sammy.runs (user_id) WHERE status IN ('queued', 'running', 'waiting');
