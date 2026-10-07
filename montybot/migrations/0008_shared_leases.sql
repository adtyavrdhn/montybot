-- Runs of one user may share the lease on the user's saved sign-ins when they share one browser, each in its own tab:
-- one row per holding run. `shared` rows of the same `owner` (the server, its EXECUTOR_ID) can be held together; any
-- other row is held alone. Rows from before this are a single exclusive holder each, as they were.
ALTER TABLE montybot.jar_leases DROP CONSTRAINT jar_leases_pkey;
ALTER TABLE montybot.jar_leases ADD COLUMN shared boolean NOT NULL DEFAULT false;
ALTER TABLE montybot.jar_leases ADD COLUMN owner text NOT NULL DEFAULT '';
ALTER TABLE montybot.jar_leases ADD PRIMARY KEY (user_id, run_id);
