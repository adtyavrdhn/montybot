-- A run the user stopped from the web app.
ALTER TABLE montybot.runs DROP CONSTRAINT runs_status_check;
ALTER TABLE montybot.runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed', 'stopped'));
