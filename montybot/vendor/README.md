# Vendored code

Copied in unchanged. Lint and type checks skip this folder.

## `claude_code`

Mike's Claude Code plugin (v0.7.1), from gist
[`bd75c7b4`](https://gist.github.com/mpfaffenberger/bd75c7b424ffdc6d81645bfe7de822d3) at revision `361680b8`.
It runs Pydantic AI on a Claude Code subscription: `ClaudeCodeModel('claude-opus-5-5')`, which montybot builds for a
`MODEL` of `claude-code:claude-opus-5-5`. `clai2.py` and `updates.py` are its CLAI2 plugin; they are only imported when
CLAI2 is installed.

It is vendored because it is not on PyPI. The PyPI package `pydantic-ai-claude-code` is a different project.

To update, replace the folder with a newer gist revision and delete `install.sh`:

```bash
rm -rf montybot/vendor/claude_code && d=$(mktemp -d)
curl -fsSL https://gist.github.com/mpfaffenberger/bd75c7b424ffdc6d81645bfe7de822d3/archive/REVISION.tar.gz | tar -xz -C "$d"
mv "$d"/* montybot/vendor/claude_code && rm montybot/vendor/claude_code/install.sh
```

### Signing in

The model authenticates with an OAuth token pair, not an API key. On a server, keep the tokens in a file on a
persistent, writable volume: Anthropic rotates the refresh token on every refresh, so the file changes while the app
runs.

```bash
export CLAUDE_CODE_CREDENTIALS=file CLAUDE_CODE_AUTH_FILE=/data/claude-code/auth.json
uv run montybot claude-code-login
```

It prints a sign-in address. Open it in any browser and sign in. On a server, the browser cannot reach the server's
localhost callback, so paste the address the browser ends on back into the terminal. (The vendored
`python -m montybot.vendor.claude_code login` only waits for the callback, so it works on a laptop only.)

Use a sign-in only this server uses. A refresh revokes the previous refresh token, so a laptop and a server sharing one
sign-in will log each other out.
