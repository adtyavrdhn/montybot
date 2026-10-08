-- Integrations (sammy.integrations): the user's own MCP servers, and runs asking the user to connect one.
-- Composio connections live in Composio, under the user's Composio id; nothing of them is stored here.

-- An MCP server the user added. `secret` holds its URL, its headers and its OAuth client and tokens, sealed with the
-- user's data key (sammy.crypto, label `<user>:mcp_server:<id>`): a URL can carry a key, so only `host` is clear.
CREATE TABLE sammy.mcp_servers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (name <> ''),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$'),
    host text NOT NULL,
    auth text NOT NULL CHECK (auth IN ('none', 'headers', 'oauth')),
    status text NOT NULL CHECK (status IN ('ready', 'needs_sign_in')),
    secret bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, slug)
);

-- An MCP OAuth sign-in on its way: the user is at the server's authorization page. `state` is the OAuth state; the
-- PKCE verifier is sealed like a server's secret (label `<user>:mcp_oauth:<state>`). Used once, then deleted.
CREATE TABLE sammy.mcp_oauth_flows (
    state text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES sammy.users (id) ON DELETE CASCADE,
    server_id uuid NOT NULL REFERENCES sammy.mcp_servers (id) ON DELETE CASCADE,
    verifier bytea NOT NULL,
    expires_at timestamptz NOT NULL
);

-- A run can ask the user to connect a service ("connect Linear"); the answer comes when it is connected.
ALTER TABLE sammy.asks DROP CONSTRAINT asks_kind_check;
ALTER TABLE sammy.asks ADD CONSTRAINT asks_kind_check
    CHECK (kind IN ('question', 'approval', 'handoff', 'connect'));
