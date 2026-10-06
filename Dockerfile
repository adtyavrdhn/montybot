# The montybot app with Chrome, Xvfb and bubblewrap, for deploy/compose.yaml.
# Playwright's image has Chromium and its system libraries; its tag matches the playwright version in uv.lock.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

# Xvfb gives each headed Chrome its own screen; bwrap jails each Chrome (`montybot.engines:chromium_server`); socat
# carries its connections out of the jail to the egress proxy (montybot/browser/egress.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb bubblewrap socat \
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
RUN mkdir -p /data/claude-code /data/workspaces && chown pwuser:pwuser /data/claude-code /data/workspaces
USER pwuser

CMD ["sh", "-c", "montybot migrate && exec montybot serve"]
