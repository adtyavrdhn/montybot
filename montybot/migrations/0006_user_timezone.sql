-- The user's time zone, as their browser reports it, so the agent knows what "today" and "9am" mean for them.
ALTER TABLE montybot.users ADD COLUMN timezone text NOT NULL DEFAULT 'UTC';
