# The montybot app with Chrome, Xvfb and bubblewrap, for deploy/compose.yaml.
# Playwright's image has Chromium and its system libraries; its tag matches the playwright version in uv.lock.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

# Xvfb gives each headed Chrome its own screen; bwrap jails Chrome and each `run_python` call.
# socat carries Chrome's connections to the egress proxy; CPython uses system pandas and pypdf.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb bubblewrap socat python3-pandas python3-pypdf \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY montybot ./montybot
RUN uv sync --frozen --no-dev

# The Claude Code sign-in and the users' files live on volumes mounted here; a new named volume copies this owner.
RUN mkdir -p /data/claude-code /data/workspaces /run/browser-egress \
    && chown pwuser:pwuser /data/claude-code /data/workspaces /run/browser-egress
USER pwuser

# Last, so a new commit rebuilds nothing else. Logfire reports it as service.version.
ARG COMMIT
ENV COMMIT=${COMMIT}

CMD ["sh", "-c", "montybot migrate && exec montybot serve"]
