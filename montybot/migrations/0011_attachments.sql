-- Files in chats (montybot.attachments): what the user attaches to a message, and what Monty shares with its reply.
-- An upload has no run until the user sends it; one never sent is deleted after a day.
CREATE TABLE montybot.attachments (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES montybot.users (id) ON DELETE CASCADE,
    run_id uuid REFERENCES montybot.runs (id) ON DELETE CASCADE,
    sender text NOT NULL CHECK (sender IN ('user', 'monty')),
    name text NOT NULL CHECK (name <> ''),
    media_type text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('image', 'pdf', 'text', 'file')),
    size bigint NOT NULL,
    data bytea NOT NULL,
    -- An image made small enough for the model; null when `data` is.
    for_model bytea,
    -- Its place among the message's files, as the user added them.
    position smallint NOT NULL DEFAULT 0,
    -- Where the run's code sees it (`/work/uploads/<name>`), once the run has started.
    path text,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Files are mostly compressed already (images, PDFs): Postgres need not try again.
ALTER TABLE montybot.attachments ALTER COLUMN data SET STORAGE EXTERNAL;
ALTER TABLE montybot.attachments ALTER COLUMN for_model SET STORAGE EXTERNAL;
CREATE INDEX attachments_by_run ON montybot.attachments (run_id, created_at);
CREATE INDEX attachments_unsent ON montybot.attachments (created_at) WHERE run_id IS NULL;
