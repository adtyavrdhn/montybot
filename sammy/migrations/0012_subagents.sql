-- Subagents (sammy.subagents): a run can start child runs that work side by side, each in a tab of its own. A child
-- is in its parent's thread, for the same user; its answer goes to the parent run, not to the chat.
ALTER TABLE sammy.runs ADD COLUMN parent_run_id uuid REFERENCES sammy.runs (id) ON DELETE CASCADE;
CREATE INDEX runs_by_parent ON sammy.runs (parent_run_id) WHERE parent_run_id IS NOT NULL;
-- Still one unfinished run per thread, not counting that run's subagents.
DROP INDEX sammy.runs_one_active_per_thread;
CREATE UNIQUE INDEX runs_one_active_per_thread ON sammy.runs (thread_id)
    WHERE status IN ('queued', 'running', 'waiting') AND parent_run_id IS NULL;
