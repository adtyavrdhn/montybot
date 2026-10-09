-- Compacted chat history (sammy.history): a summary of the thread's oldest messages, the ones before position
-- `compacted_up_to`. Runs get the summary and the messages from `compacted_up_to` on; the messages themselves stay.
ALTER TABLE sammy.threads ADD COLUMN summary text NOT NULL DEFAULT '';
ALTER TABLE sammy.threads ADD COLUMN compacted_up_to integer NOT NULL DEFAULT 0;
