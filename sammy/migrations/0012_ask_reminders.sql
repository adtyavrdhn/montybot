-- When Sammy reminded the user about an ask they left waiting (sammy.reminders). Set once, so each question,
-- approval or hand-off gets one reminder at most.
ALTER TABLE sammy.asks ADD COLUMN reminded_at timestamptz;
-- The reminder sweep reads only the asks still open and not yet reminded.
CREATE INDEX asks_to_remind ON sammy.asks (created_at) WHERE answer IS NULL AND reminded_at IS NULL;
