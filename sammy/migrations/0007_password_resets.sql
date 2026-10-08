-- Codes for resetting a forgotten password: emailed to the account's address, stored only as a hash.
CREATE TABLE sammy.password_resets (
    user_id uuid PRIMARY KEY REFERENCES sammy.users (id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0
);
