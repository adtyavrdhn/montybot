# Sammy for Mac. `make` lists what you can do; `make run` is all you need to try it locally.
.DEFAULT_GOAL := help
SERVER ?= https://35-188-200-101.sslip.io
LOCAL ?= http://127.0.0.1:8000
REPO := $(abspath ..)
BLENDER ?= /Applications/Blender.app/Contents/MacOS/Blender

.PHONY: help setup dev app run install test journeys tour share mascot clean

help:  ## What you can do
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'
	@echo "  SERVER=https://sammy.example.com make app   builds an app for that server (default $(SERVER))"

setup:  ## Check the tools (Xcode, uv) and install the server's Python packages
	@xcode-select -p >/dev/null || (echo "Install Xcode, or run: xcode-select --install" && exit 1)
	@command -v uv >/dev/null || (echo "Install uv: https://docs.astral.sh/uv/" && exit 1)
	cd $(REPO) && uv sync
	cd $(REPO) && uv run playwright install chromium

dev:  ## For development only: a local sammy with a demo model (make app SERVER=$(LOCAL) to use it)
	cd $(REPO) && uv run python macos/scripts/dev_server.py

app:  ## Build build/Sammy.app for SERVER
	scripts/build-app.sh $(SERVER)

run: install  ## Build, install and open the app
	open /Applications/Sammy.app

install: app  ## Put the app in /Applications, the one Sammy on this Mac (no second copy left in build/)
	@osascript -e 'quit app id "dev.pydantic.sammy"' 2>/dev/null || true
	@# Copied next to the installed app first: a copy that fails leaves the working one in place.
	rm -rf /Applications/.Sammy-new.app
	ditto build/Sammy.app /Applications/.Sammy-new.app
	rm -rf /Applications/Sammy.app
	mv /Applications/.Sammy-new.app /Applications/Sammy.app
	rm -rf build/Sammy.app
	@echo "Installed /Applications/Sammy.app (talks to $(SERVER))"

test:  ## Unit tests, and the journeys too when 'make dev' is running (never against the real server)
	@if curl -fs $(LOCAL)/healthz >/dev/null; then SAMMY_TEST_SERVER=$(LOCAL) swift test; \
	else echo "No dev server at $(LOCAL): unit tests only (run 'make dev' in another terminal for the journeys)"; swift test; fi

tour:  ## Every screen as PNG and text in build/tour (needs 'make dev' and an unlocked Mac)
	@# Whether the tour works or not, no second Sammy is left next to the installed one.
	scripts/build-app.sh $(SERVER) && SAMMY_TOUR_SERVER=$(LOCAL) scripts/tour.sh build/tour; status=$$?; rm -rf build/Sammy.app; exit $$status

share: app  ## build/Sammy.zip to send to someone (ad hoc signed; see README for Developer ID)
	cd build && rm -f Sammy.zip && ditto -c -k --keepParent Sammy.app Sammy.zip
	@echo "build/Sammy.zip: unzip, then right-click Sammy.app > Open the first time (it isn't notarized)."

mascot:  ## Re-render the squirrel's loops into Resources/Squirrel (needs Blender and uv; see mascot/README.md)
	cd mascot && $(BLENDER) -b --python build_sammysquirrel.py --python-expr \
		"import bpy, os; bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath('sammysquirrel.blend'))"
	cd mascot && $(BLENDER) -b sammysquirrel.blend --python render_states.py -- --frames
	cd mascot && uv run make_loops.py frames ../Resources/Squirrel --name sammysquirrel --apng-size 192 --no-gif

clean:  ## Remove builds
	rm -rf build .build
