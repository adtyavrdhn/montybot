# The app image: the web app, its DBOS workflows and the browser service (Chromium in bwrap, an Xvfb per browser).
# Builds for linux/amd64 and linux/arm64. No secrets: every setting comes from the environment at run time.
FROM python:3.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PATH=/opt/venv/bin:$PATH

# bwrap jails each Chrome, Xvfb is its screen, socat carries its connections out of the jail (montybot/browser/egress.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap xvfb socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
# Full Chromium with the libraries and fonts it needs; production runs headed, so no headless shell.
RUN playwright install --with-deps --no-shell chromium && rm -rf /var/lib/apt/lists/*

COPY montybot ./montybot
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 1000 montybot
USER montybot
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "montybot migrate && exec montybot serve"]
