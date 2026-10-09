-- Messages the user sent while a run was working (sammy.steering). The run reads them before its next model request.
CREATE TABLE sammy.steering (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES sammy.runs (id) ON DELETE CASCADE,
    text text NOT NULL CHECK (text <> ''),
    -- How many asks the run had made when it was sent, so the chat shows it after those.
    after_asks integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Which of the run's model requests read it (`sammy.steering.read`); null until one has.
    read_by integer
);
CREATE INDEX steering_by_run ON sammy.steering (run_id, id);
