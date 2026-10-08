-- What the user named their squirrel, the mascot beside the Mac app's composer: Sammy answers to it. Empty until they
-- pick one. The app sends it with each message, as it does the time zone; scheduled runs use the last one sent.
ALTER TABLE sammy.users ADD COLUMN squirrel_name text NOT NULL DEFAULT '';
