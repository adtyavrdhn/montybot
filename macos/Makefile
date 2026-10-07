# Monty for Mac. `make` lists what you can do; `make run` is all you need to try it locally.
.DEFAULT_GOAL := help
SERVER ?= http://127.0.0.1:8000
REPO := $(abspath ..)

.PHONY: help setup dev app run test journeys tour share clean

help:  ## What you can do
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'
	@echo "  SERVER=https://monty.example.com make app   builds an app for that server (default $(SERVER))"

setup:  ## Check the tools (Xcode, uv) and install the server's Python packages
	@xcode-select -p >/dev/null || (echo "Install Xcode, or run: xcode-select --install" && exit 1)
	@command -v uv >/dev/null || (echo "Install uv: https://docs.astral.sh/uv/" && exit 1)
	cd $(REPO) && uv sync
	cd $(REPO) && uv run playwright install chromium

dev:  ## Run a local montybot for the app (scripted model, fixture sites, embedded Postgres; Ctrl-C stops it)
	cd $(REPO) && uv run python macos/scripts/dev_server.py

app:  ## Build build/Monty.app for SERVER
	scripts/build-app.sh $(SERVER)

run: app  ## Build and open the app
	open build/Monty.app

test:  ## Unit tests, and the journeys too when a dev server is up
	@if curl -fs $(SERVER)/healthz >/dev/null; then MONTY_TEST_SERVER=$(SERVER) swift test; \
	else echo "No server at $(SERVER): unit tests only (run 'make dev' in another terminal for the journeys)"; swift test; fi

tour: app  ## Every screen as PNG and text in build/tour (needs 'make dev' and an unlocked Mac)
	scripts/tour.sh build/tour

share: app  ## build/Monty.zip to send to someone (ad hoc signed; see README for Developer ID)
	cd build && rm -f Monty.zip && ditto -c -k --keepParent Monty.app Monty.zip
	@echo "build/Monty.zip: unzip, then right-click Monty.app > Open the first time (it isn't notarized)."

clean:  ## Remove builds
	rm -rf build .build
