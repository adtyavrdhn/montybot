-- What each model request cost (sammy.usage): the tokens and the price genai-prices gives them, as Logfire shows.
-- A row outlives its run and chat (run_id goes null), so deleting a chat does not take back what it spent.
CREATE TABLE sammy.usage (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    run_id uuid REFERENCES sammy.runs (id) ON DELETE SET NULL,
    -- The run's n-th model request (Pydantic AI's run step): a replayed workflow records the same request once.
    request integer NOT NULL,
    model text NOT NULL,
    input_tokens bigint NOT NULL,
    output_tokens bigint NOT NULL,
    cache_read_tokens bigint NOT NULL,
    cache_write_tokens bigint NOT NULL,
    -- In US dollars; null when genai-prices does not know the model.
    cost numeric(20, 10),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, request)
);
CREATE INDEX usage_by_user ON sammy.usage (user_id, created_at);
