-- Skills (sammy.skills): a user's playbooks for tasks Sammy has been shown or told how to do. The agent sees a short
-- index (names and when to use them) and loads a whole skill with `load_skill`. A draft, made from a lesson in the
-- live view ("Teach Sammy"), is not shown to the agent until the user reviews and saves it.
CREATE TABLE sammy.skills (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name <> ''),
    when_to_use text NOT NULL CHECK (when_to_use <> ''),
    inputs text NOT NULL DEFAULT '',
    steps text NOT NULL CHECK (steps <> ''),
    -- How to check it worked.
    verify text NOT NULL DEFAULT '',
    -- What to give back to the user.
    returns text NOT NULL DEFAULT '',
    -- What needs the user's say-so first.
    approvals text NOT NULL DEFAULT '',
    -- What to do when a step fails.
    failures text NOT NULL DEFAULT '',
    draft boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- `load_skill` finds a skill by name, so a user's saved skills have different names. Drafts may repeat one.
CREATE UNIQUE INDEX skills_by_name ON sammy.skills (user_id, lower(name)) WHERE NOT draft;
CREATE INDEX skills_by_user ON sammy.skills (user_id, created_at);
