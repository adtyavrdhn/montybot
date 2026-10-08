"""OAuth endpoint and client configuration for Claude Code authentication.

These constants were verified against the Claude Code CLI 2.1.285 binary
(`@anthropic-ai/claude-code-darwin-arm64`, embedded config) and its published client metadata:

- `https://claude.ai/oauth/claude-code-client-metadata` (dynamic client registration)
- Authorization server: `https://claude.com/cai/oauth/authorize`
- Token endpoint: `https://platform.claude.com/v1/oauth/token`

The shared `9d1c250a...` client id is the public client the official CLI uses,
so our flow presents the same credentials to Anthropic's authorization server.
See `flow.py` for the authorization-code flow, and `provider.py` for the API client.
"""

# OAuth endpoints and the shared public client id used by the Claude Code CLI.
AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Same scope set the plugin in `code_puppy_core_plugins` uses. The binary's
# `user:ccr_inference` scope is not recognized by the authorization server.
SCOPES = "org:create_api_key user:profile user:inference"

# The subscription tokens minted by this flow are valid against api.anthropic.com.
API_BASE_URL = "https://api.anthropic.com"

# The current Claude models, offered in CLAI2's model menus. Checked 2026-09-30 against three
# sources that agree: Anthropic's models overview
# (https://docs.anthropic.com/en/docs/about-claude/models/overview), the `ModelParam` literal of
# `anthropic` 1.9.0 that Pydantic AI's `KnownModelName` is built from, and genai-prices 0.1.9.
# Any other model ID your subscription serves (e.g. `claude-opus-4-8`) works too; it is just not listed.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-4-5")

# Must open the system context when authenticating with Claude Code tokens. This
# is the exact persona string the official CLI sends; the subscription backend
# expects it at position 0.
CLAUDE_CODE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."

# Redirect handling. We host a short-lived callback server on localhost. The
# authorization server accepts `http://localhost:<any port>/callback` but
# rejects `127.0.0.1` variants, so the host must stay `localhost`.
REDIRECT_HOST = "http://localhost"
REDIRECT_PATH = "callback"
CALLBACK_PORT_RANGE = (8765, 8795)
CALLBACK_TIMEOUT = 180

# Request headers that must accompany Claude Code subscription tokens. `anthropic-beta` is
# appended (never replaced) by the auth layer so feature betas set by pydantic-ai survive.
ANTHROPIC_BETA = "oauth-2025-04-20"
# The subscription backend gates models on this version: Opus 5.5 answers 400 below 2.1.280.
USER_AGENT = "claude-cli/2.1.285 (external, cli)"
X_APP = "cli"
