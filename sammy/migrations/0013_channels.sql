-- Chat apps (sammy.channels): people message Sammy from Slack, Telegram, WhatsApp or Discord.

-- A platform account tied to a Sammy user. Pings go only to `notify_chat_id`, the direct chat they linked from.
CREATE TABLE sammy.channel_identities (
    channel text NOT NULL,
    external_user_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    notify_chat_id text,
    notify boolean NOT NULL DEFAULT true,
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, external_user_id)
);
CREATE INDEX channel_identities_by_user ON sammy.channel_identities (user_id);

-- One-time link codes, by their SHA-256 only. Either issued to an unknown sender (`external_user_id` set, claimed by
-- a signed-in web user) or issued to a web user (`user_id` set, sent to the bot from the platform). A sender's code is
-- derived from `nonce` with the session secret, so a live one can be sent again without storing it.
CREATE TABLE sammy.channel_link_codes (
    code_hash text PRIMARY KEY,
    channel text NOT NULL,
    external_user_id text,
    chat_id text,
    nonce text,
    user_id uuid REFERENCES sammy.users (id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    -- A sender's code (opened on the web), a web user's code (sent to the bot), or both: the code a web user got for
    -- a sender's code, which only that sender can send to finish linking.
    CHECK (external_user_id IS NOT NULL OR user_id IS NOT NULL)
);
CREATE INDEX channel_link_codes_by_sender ON sammy.channel_link_codes (channel, external_user_id)
    WHERE external_user_id IS NOT NULL;

-- A platform conversation is one Sammy thread per linked user, so a shared thread never mixes two users' data.
CREATE TABLE sammy.channel_chats (
    channel text NOT NULL,
    chat_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    thread_id uuid NOT NULL REFERENCES sammy.threads (id) ON DELETE CASCADE,
    PRIMARY KEY (channel, chat_id, user_id)
);

-- Runs started from a chat: their asks and their reply go back there.
CREATE TABLE sammy.channel_runs (
    run_id uuid PRIMARY KEY REFERENCES sammy.runs (id) ON DELETE CASCADE,
    channel text NOT NULL,
    chat_id text NOT NULL
);

-- What to send, in order per chat. The id is derived from what the row is for (a run's reply, an ask, a ping), so a
-- producer that runs twice adds it once.
CREATE TABLE sammy.channel_outbox (
    id uuid PRIMARY KEY,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    channel text NOT NULL,
    chat_id text NOT NULL,
    user_id uuid REFERENCES sammy.users (id) ON DELETE CASCADE,
    ask_id uuid,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    message_ids jsonb,
    failed_at timestamptz,
    error text
);
CREATE INDEX channel_outbox_unsent ON sammy.channel_outbox (channel, chat_id, seq)
    WHERE sent_at IS NULL AND failed_at IS NULL;
CREATE INDEX channel_outbox_by_ask ON sammy.channel_outbox (ask_id) WHERE ask_id IS NOT NULL;
