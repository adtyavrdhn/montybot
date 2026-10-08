-- What the user named their squirrel, the mascot beside the Mac app's composer: Monty answers to it. Empty until they
-- pick one. The app sends it with each message, as it does the time zone; scheduled runs use the last one sent.
ALTER TABLE montybot.users ADD COLUMN squirrel_name text NOT NULL DEFAULT '';
