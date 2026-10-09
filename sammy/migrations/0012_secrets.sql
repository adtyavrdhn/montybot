-- Secrets the agent can use but never sees (sammy.vault): an API token or a webhook URL the user typed into a
-- `request_secret` card. `value` is sealed with the user's data key (sammy.crypto, label
-- `<user>:secret:<name>:<host>`), so a value copied to another row, or a host changed here, does not open. The name
-- and the one host it may be sent to are in the clear, for lists.
CREATE TABLE sammy.secrets (
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_]{0,63}$'),
    host text NOT NULL CHECK (host <> ''),
    value bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, name)
);

-- A run can ask the user for a secret; the answer says only whether they saved one.
ALTER TABLE sammy.asks DROP CONSTRAINT asks_kind_check;
ALTER TABLE sammy.asks ADD CONSTRAINT asks_kind_check
    CHECK (kind IN ('question', 'approval', 'handoff', 'connect', 'secret'));
