-- Preferences are user-scoped; each run keeps its original resolved selection through replay.
CREATE TABLE sammy.model_preferences (
    user_id uuid PRIMARY KEY REFERENCES sammy.users (id) ON DELETE CASCADE,
    model text NOT NULL,
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings) = 'object')
);
CREATE TABLE sammy.run_models (
    run_id uuid PRIMARY KEY REFERENCES sammy.runs (id) ON DELETE CASCADE,
    selection jsonb NOT NULL CHECK (jsonb_typeof(selection) = 'object')
);
