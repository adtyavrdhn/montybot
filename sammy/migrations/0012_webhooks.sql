-- Webhook triggers (sammy.webhooks): a task that starts when another service sends a signed event to the webhook's
-- URL. Every event runs in the webhook's own thread. The URL's token is kept only as its SHA-256. The signing secret
-- checks every request, so it is sealed with the user's data key (sammy.crypto, label `<user>:webhook:<id>`).
CREATE TABLE sammy.webhooks (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    thread_id uuid NOT NULL UNIQUE REFERENCES sammy.threads (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name <> ''),
    prompt text NOT NULL CHECK (prompt <> ''),
    -- How requests are signed: GitHub's own scheme, or ours (`X-Sammy-Signature`) for any other sender.
    source text NOT NULL CHECK (source IN ('github', 'hmac')),
    token_hash text NOT NULL UNIQUE,
    secret bytea NOT NULL,
    paused boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhooks_user_idx ON sammy.webhooks (user_id, created_at);

-- Each event a webhook took, by the sender's delivery id, so a replay or a redelivery starts no second run.
-- `digest` (the body's SHA-256) is kept for GitHub, which does not sign its delivery id; null for other senders.
CREATE TABLE sammy.webhook_deliveries (
    webhook_id uuid NOT NULL REFERENCES sammy.webhooks (id) ON DELETE CASCADE,
    delivery_id text NOT NULL,
    digest bytea,
    run_id uuid NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (webhook_id, delivery_id),
    UNIQUE (webhook_id, digest)
);

ALTER TABLE sammy.runs DROP CONSTRAINT runs_trigger_check;
ALTER TABLE sammy.runs ADD CONSTRAINT runs_trigger_check CHECK (trigger IN ('message', 'schedule', 'webhook'));
