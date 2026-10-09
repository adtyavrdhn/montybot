-- Reply windows (sammy.channels, #141): some platforms (WhatsApp) let the bot write freely only for a while after the
-- chat's last message. For a platform with one, each chat's last message is kept here, and when the platform's
-- re-engagement message was last sent, so it goes once per closed window.
CREATE TABLE sammy.channel_windows (
    channel text NOT NULL,
    chat_id text NOT NULL,
    last_inbound_at timestamptz,
    reopened_at timestamptz,
    PRIMARY KEY (channel, chat_id)
);

-- A message held while its chat's window is closed waits (`held_at`) until the chat writes again. Each release is a
-- new delivery workflow (`channel-send:<row id>:<releases>`), as the one that held it has finished.
ALTER TABLE sammy.channel_outbox
    ADD COLUMN held_at timestamptz,
    ADD COLUMN releases integer NOT NULL DEFAULT 0;
DROP INDEX sammy.channel_outbox_unsent;
CREATE INDEX channel_outbox_unsent ON sammy.channel_outbox (channel, chat_id, seq)
    WHERE sent_at IS NULL AND failed_at IS NULL AND held_at IS NULL;
