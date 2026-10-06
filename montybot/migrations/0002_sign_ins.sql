-- Each user's saved sign-ins (the browser's `BrowserState`), encrypted with the user's own data key, and the lease
-- that lets one run at a time use and save them.

-- The user's data key, encrypted with the deployment's key (montybot.crypto). Made on first use.
ALTER TABLE montybot.users ADD COLUMN data_key bytea;

CREATE TABLE montybot.sign_ins (
    user_id uuid PRIMARY KEY REFERENCES montybot.users (id) ON DELETE CASCADE,
    version integer NOT NULL,
    state bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE montybot.jar_leases (
    user_id uuid PRIMARY KEY REFERENCES montybot.users (id) ON DELETE CASCADE,
    run_id text NOT NULL,
    expires_at timestamptz NOT NULL
);
