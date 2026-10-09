-- Remembered approvals (sammy.approval_rules): "Always allow this" on an approval card. A rule covers one action: a
-- tool, where (`scope`: the site's host for `commit`, the integration's key for `call_integration_tool`) and what
-- (`name`: the control clicked, or the integration's tool). An action that spends money, sends as the user or deletes
-- (`risk`) is covered only if the user said so for that rule (`allow_risky`).
CREATE TABLE sammy.approval_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    tool text NOT NULL CHECK (tool IN ('commit', 'call_integration_tool')),
    scope text NOT NULL CHECK (scope <> ''),
    name text NOT NULL CHECK (name <> ''),
    risk text CHECK (risk IN ('money', 'send', 'delete')),
    allow_risky boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, tool, scope, name)
);

-- The users who let the automatic reviewer (sammy.reviewer) approve low-risk actions no rule covers.
CREATE TABLE sammy.approval_reviewers (
    user_id uuid PRIMARY KEY REFERENCES sammy.users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);
