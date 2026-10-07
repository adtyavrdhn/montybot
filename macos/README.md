# Monty for Mac

A native macOS app (SwiftUI, macOS 14+) for montybot: chat with Monty, watch its browser, answer its questions and
approvals, and take over its browser to sign in. Monty itself runs on the montybot server; the app is its client, as
the web app is.

```
Sources/MontyKit   everything but the views: the API (APIClient), a run's live updates (EventStream), the live view's
                   WebSocket (LiveWire, LiveSession), Markdown blocks, and the app's state (AppModel, ChatModel)
Sources/Monty      the SwiftUI app: windows, sidebar, chat, takeover, library pages, menu bar, Settings, and Theme
                   (Logfire's design tokens)
Tests              unit tests, and journeys: a person using the app's models against a real server
scripts            dev_server.py, build-app.sh, make-icon.swift, tour.sh and ax-dump.swift
```

## Run it

```bash
cd macos
make run       # builds and opens Monty.app, talking to the deployed server (it asks once for its site login)
make share     # build/Monty.zip to send to someone
make           # everything else: setup, dev (a local server for development), test, tour, clean
```

To give someone the app: `SERVER=https://monty.example.com make share` makes `build/Monty.zip`. It is signed ad
hoc, so the first time they right-click Monty.app and choose Open. With a Developer ID, sign and notarize instead
(commands in `scripts/build-app.sh`) and it opens like any app.

`dev_server.py` needs no Docker: without `--postgres` it starts an embedded Postgres in `data/mac-dev-pg` (the
`pgserver` package), and it prints the prompts the scripted model knows, with the fixture sites' addresses. With
`MONTYBOT_TEST_MODEL` set (and its API key), a real model answers instead.

`scripts/build-app.sh https://monty.example.com` builds an app that talks to that server; users can change it in
Settings. The app is signed ad hoc; for users, sign it with a Developer ID and notarize it (see the script).

## Test it

```bash
swift test                                                   # unit tests (wire formats, keys, Markdown, models)
MONTY_TEST_SERVER=http://127.0.0.1:8000 swift test           # and the journeys, against the dev server
```

The journeys sign up a new person each and go through what a person does: a chat and its reply, a question, signing
in through the live view and approving an order, stopping, notifications from other chats, drafts, sign-out, an
ended session, schedules, files, and the races found in review (double sends, answers closed elsewhere, leaving a
chat mid-send, double takeovers, navigating away mid sign-in).

## Look at it

```bash
scripts/tour.sh build/tour      # needs the dev server, and an unlocked Mac
```

The tour drives the real app through every screen (sign-in, new task, working, a reply with a table, each kind of
question, the takeover, the sidebar with chats that need you, stopped and failed tasks, the library pages, dark
mode, a narrow window) as a new person, and saves each as `NN-name.png` and `NN-name.txt`: what VoiceOver reads
there, with every element's frame, so a review can check wording, labels, alignment and clipping without clicking
through. `ALL.txt` has every screen in one file.

## Design

Monty looks like Logfire: its tokens (platform `src/services/logfire-frontend/src/styles/design-tokens.css`) are in
`Theme.swift` with their Logfire names. Neutral surfaces and hairline outlines; one control height (32, or 28 small)
and radius (8 containers, 6 controls); blue for anything you act on (a step deeper than Logfire's `--link`, so
white text on it passes 4.5:1); Pydantic pink only for the brand and the "needs you" dot; the system font, and mono
for metadata. Native where it matters: a real sidebar list, the window's own title, menus with shortcuts, a menu bar
menu, Notification Center and the Dock badge for chats that need you.

Decisions from review:

- Stop never sits where Send is (it is in the toolbar and Task menu, ⌘., and asks first). Approving has no keyboard
  shortcut: going ahead with something that costs money takes a click.
- While Monty waits for an answer the composer is off and says why; typed answers survive the question closing,
  switching chats, and being answered elsewhere.
- Taking over fills the window, and nothing (⌘N, a menu, a notification) takes the user away mid sign-in; ⇧⌘T leaves,
  ⌘↩ hands back. Typing goes through macOS text input, so accents and input methods work; ⌘V pastes from the Mac.
  With VoiceOver, the remote page reads like a web page: the server sends its outline (headings, text, fields,
  buttons, with where they are; passwords only as a count), and activating an item clicks it.
- Chats can be renamed and deleted (context menu, ⌘⌫); failed and stopped chats are marked in the sidebar.
- Monty's steps stay under its reply, folded.

## Not yet

- Password reset emails a 6-digit code through the server's `SMTP_URL`; without one, no code is sent (set it for
  real users). Codes last 15 minutes and 5 tries.
- Without a Developer ID, the shared app is signed ad hoc (right-click > Open the first time).
- Notifications come from the app while it runs; "Open at login" (Settings) keeps it in the menu bar. Reaching a Mac
  where Monty isn't running would need a push service (APNs).
- VoiceOver reads remote pages on Chromium (the live view's outline); Servo's live view can't read pages yet and says
  so. The outline covers what is on screen: scroll to reach the rest.
