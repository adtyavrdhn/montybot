# Vendored code

Third-party code, with provenance and local adaptations recorded below. Lint and type checks skip
`claude_code`; `clai2_models` is checked with Sammy's normal rules.

## `claude_code`

Mike's Claude Code plugin (v0.7.1), from gist
[`bd75c7b4`](https://gist.github.com/mpfaffenberger/bd75c7b424ffdc6d81645bfe7de822d3) at revision `361680b8`.
It runs Pydantic AI on a Claude Code subscription: `ClaudeCodeModel('claude-opus-5-5')`, which sammy builds for a
`MODEL` of `claude-code:claude-opus-5-5`. `clai2.py` and `updates.py` are its CLAI2 plugin; they are only imported when
CLAI2 is installed.

It is vendored because it is not on PyPI. The PyPI package `pydantic-ai-claude-code` is a different project.

To update, replace the folder with a newer gist revision and delete `install.sh`:

```bash
rm -rf sammy/vendor/claude_code && d=$(mktemp -d)
curl -fsSL https://gist.github.com/mpfaffenberger/bd75c7b424ffdc6d81645bfe7de822d3/archive/REVISION.tar.gz | tar -xz -C "$d"
mv "$d"/* sammy/vendor/claude_code && rm sammy/vendor/claude_code/install.sh
```

### Signing in

The model authenticates with an OAuth token pair, not an API key. On a server, keep the tokens in a file on a
persistent, writable volume: Anthropic rotates the refresh token on every refresh, so the file changes while the app
runs.

```bash
export CLAUDE_CODE_CREDENTIALS=file CLAUDE_CODE_AUTH_FILE=/data/claude-code/auth.json
uv run sammy claude-code-login
```

It prints a sign-in address. Open it in any browser and sign in. On a server, the browser cannot reach the server's
localhost callback, so paste the address the browser ends on back into the terminal. (The vendored
`python -m sammy.vendor.claude_code login` only waits for the callback, so it works on a laptop only.)

Use a sign-in only this server uses. A refresh revokes the previous refresh token, so a laptop and a server sharing one
sign-in will log each other out.

## `clai2_models` (issue #143)

Vendored on 2026-10-09 from `/Users/mpfaffenberger/code/pydantic-ai`, commit
`4356af5d3`, under `src/pydantic_clai2/pydantic_clai2/models/`:

- `model_settings.py`: `ModelSettingsForm`, `model_defaults`, `default_model_settings`, and
  `model_settings_from_json`. Preserves strict edit validation, forward-compatible saved-setting reads,
  family defaults, native OpenAI/Anthropic conversion, GLM body settings, and custom parameter precedence.
- `model_options.py`: `model_options` and `validate_model_options`, including model-specific choices,
  thinking budget and sampling constraints, and effort validation.
- `model_catalog.py`: `CatalogModel`, `runnable_providers`, `genai_prices_models`, and `catalog`.
  Returns sorted, deduplicated metadata from the installed Pydantic AI names and genai-prices snapshot,
  with explicit inclusions and caller-supplied discovery entries taking precedence.
- `profiles.py`: only `provider_of` and `base_model`, the two name helpers needed by the above modules.
- `custom_params.py`: only `expand_params`, required for dotted-key validation and request conversion.

### Local adaptations

Imports are package-relative and formatting follows Sammy. There are no TUI, plugin, credential-store,
network discovery, or account-management dependencies. Removed Copilot discovery, the hardcoded Codex
subscription catalog, CLAI extra-provider declarations, and CLAI-specific SDK installation advice
(`check_installed`). `runnable_providers` uses core provider names plus Sammy's `claude-code`; it does
not promise installed SDKs or credentials. Pass the configured subscription model via `catalog(include=...)`.
The `discovered` merge argument remains a pure-data API, not an authenticated discovery mechanism.
Settings choices retain upstream provider-specific behavior; they do not install or enable providers.
Name helpers do not add auth-profile execution support to Sammy.

Checked against installed `pydantic-ai-slim 2.54.0` and `anthropic 1.11.0`: native settings TypedDicts,
Anthropic thinking/binding types, profile capability keys, and `known_model_names()` are compatible,
so no SDK compatibility shim or change to settings conversion was needed.
Sammy's picker policy on top of it is `sammy/model_preferences.py`.

To update, extract the same files from a reviewed commit, retain the minimal helper subset and catalog
adaptations above, and recheck conversion and validation before changing this provenance. Run:

```bash
.venv/bin/ruff format sammy/vendor/clai2_models
.venv/bin/ruff check sammy/vendor/clai2_models
.venv/bin/pyright --pythonpath .venv/bin/python sammy/vendor/clai2_models
```

### Upstream license

The MIT License (MIT)

Copyright (c) Pydantic Services Inc. 2024 to present

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
