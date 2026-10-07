# Monty for Mac. `make` lists what you can do; `make run` is all you need to try it locally.
.DEFAULT_GOAL := help
SERVER ?= https://35-188-200-101.sslip.io
LOCAL ?= http://127.0.0.1:8000
REPO := $(abspath ..)

.PHONY: help setup dev app run install test journeys tour share clean

help:  ## What you can do
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'
	@echo "  SERVER=https://monty.example.com make app   builds an app for that server (default $(SERVER))"

setup:  ## Check the tools (Xcode, uv) and install the server's Python packages
	@xcode-select -p >/dev/null || (echo "Install Xcode, or run: xcode-select --install" && exit 1)
	@command -v uv >/dev/null || (echo "Install uv: https://docs.astral.sh/uv/" && exit 1)
	cd $(REPO) && uv sync
	cd $(REPO) && uv run playwright install chromium

dev:  ## For development only: a local montybot with a demo model (make app SERVER=$(LOCAL) to use it)
	cd $(REPO) && uv run python macos/scripts/dev_server.py

app:  ## Build build/Monty.app for SERVER
	scripts/build-app.sh $(SERVER)

run: install  ## Build, install and open the app
	open /Applications/Monty.app

install: app  ## Put the app in /Applications, the one Monty on this Mac (no second copy left in build/)
	@osascript -e 'quit app id "dev.pydantic.monty"' 2>/dev/null || true
	rm -rf /Applications/Monty.app
	ditto build/Monty.app /Applications/Monty.app
	rm -rf build/Monty.app
	@echo "Installed /Applications/Monty.app (talks to $(SERVER))"

test:  ## Unit tests, and the journeys too when 'make dev' is running (never against the real server)
	@if curl -fs $(LOCAL)/healthz >/dev/null; then MONTY_TEST_SERVER=$(LOCAL) swift test; \
	else echo "No dev server at $(LOCAL): unit tests only (run 'make dev' in another terminal for the journeys)"; swift test; fi

tour:  ## Every screen as PNG and text in build/tour (needs 'make dev' and an unlocked Mac)
	@# Whether the tour works or not, no second Monty is left next to the installed one.
	scripts/build-app.sh $(SERVER) && MONTY_TOUR_SERVER=$(LOCAL) scripts/tour.sh build/tour; status=$$?; rm -rf build/Monty.app; exit $$status

share: app  ## build/Monty.zip to send to someone (ad hoc signed; see README for Developer ID)
	cd build && rm -f Monty.zip && ditto -c -k --keepParent Monty.app Monty.zip
	@echo "build/Monty.zip: unzip, then right-click Monty.app > Open the first time (it isn't notarized)."

clean:  ## Remove builds
	rm -rf build .build
