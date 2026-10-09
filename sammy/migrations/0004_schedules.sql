-- Grown from viktor c1896df (migration scheduled_tasks). The user's schedules: each is a DBOS schedule named
-- `sammy-schedule-<id>` (sammy.schedules), which fires and pauses; this row says whose it is and what it does.
-- Every occurrence runs in the schedule's own thread.
CREATE TABLE sammy.schedules (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    thread_id uuid NOT NULL UNIQUE REFERENCES sammy.threads (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name <> ''),
    cron text NOT NULL,
    timezone text NOT NULL,
    when_text text NOT NULL,
    prompt text NOT NULL CHECK (prompt <> ''),
    -- A watch tells the user only when it finds what they wait for, then pauses.
    watch boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX schedules_user_idx ON sammy.schedules (user_id, created_at);
