-- Web push subscriptions: where to send "the bot needs you" for each of a user's browsers and phones.
CREATE TABLE sammy.push_subscriptions (
    endpoint text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    keys jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX push_subscriptions_user_idx ON sammy.push_subscriptions (user_id);
