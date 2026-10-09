-- Grown from viktor c1896df (migrations 0001, 0005). Every row belongs to one user: there are no workspaces.

CREATE TABLE sammy.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE CHECK (email = lower(email) AND email <> ''),
    password_hash text NOT NULL,
    name text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sammy.threads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    title text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX threads_user_idx ON sammy.threads (user_id, created_at);

-- The model's view of a thread: Pydantic AI messages, in order.
CREATE TABLE sammy.messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES sammy.threads (id) ON DELETE CASCADE,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, position)
);

-- One agent run: one user message and everything the agent did for it. Its id is the DBOS workflow id.
CREATE TABLE sammy.runs (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    thread_id uuid NOT NULL REFERENCES sammy.threads (id) ON DELETE CASCADE,
    trigger text NOT NULL CHECK (trigger IN ('message', 'schedule')),
    prompt text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed')),
    output text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX runs_thread_idx ON sammy.runs (thread_id, created_at);
-- One unfinished run per thread.
CREATE UNIQUE INDEX runs_one_active_per_thread ON sammy.runs (thread_id)
    WHERE status IN ('queued', 'running', 'waiting');

-- What a run asked the user: a question, an approval or a browser hand-off. The run waits in DBOS.recv until
-- `answer` is set and sent to it.
CREATE TABLE sammy.asks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES sammy.runs (id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    occurrence integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('question', 'approval', 'handoff')),
    prompt text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    answer jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    answered_at timestamptz,
    UNIQUE (run_id, occurrence)
);

-- What the agent is doing, for the web app while a run is going: "Opened shop.test". Never page contents.
CREATE TABLE sammy.activity (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES sammy.runs (id) ON DELETE CASCADE,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_run_idx ON sammy.activity (run_id, id);

CREATE TABLE sammy.memories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    thread_id uuid REFERENCES sammy.threads (id) ON DELETE SET NULL,
    text text NOT NULL CHECK (text <> ''),
    search tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memories_search_idx ON sammy.memories USING gin (search);
CREATE INDEX memories_user_idx ON sammy.memories (user_id, created_at);
