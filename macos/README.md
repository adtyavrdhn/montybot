# Sammy for Mac

A native macOS app (SwiftUI, macOS 14+) for Sammy: chat with it, watch its browser, answer its questions and
approvals, and take over its browser to sign in. Sammy itself runs on the Sammy server; the app is its client, as
the web app is.

```
Sources/SammyKit   everything but the views: the API (APIClient), a run's live updates (EventStream), the live view's
                   WebSocket (LiveWire, LiveSession), Markdown blocks, and the app's state (AppModel, ChatModel)
Sources/Sammy      the SwiftUI app: windows, sidebar, chat, takeover, library pages, menu bar, Settings, and Theme
                   (Logfire's design tokens)
Tests              unit tests, and journeys: a person using the app's models against a real server
scripts            dev_server.py, build-app.sh, make-icon.swift, tour.sh and ax-dump.swift
Resources/Squirrel the mascot's loops (APNG), which build-app.sh copies into the app
mascot             how those loops are made: a Meshy mesh painted, rigged and animated in Blender (`make mascot`)
```

## Run it

```bash
cd macos
make run       # builds Sammy.app for the deployed server, installs it in /Applications and opens it
make share     # build/Sammy.zip to send to someone
make           # everything else: setup, dev (a local server for development), test, tour, clean
```

To give someone the app: `SERVER=https://sammy.example.com make share` makes `build/Sammy.zip`. It is signed ad
hoc, so the first time they right-click Sammy.app and choose Open. With a Developer ID, sign and notarize instead
(commands in `scripts/build-app.sh`) and it opens like any app.

`dev_server.py` needs no Docker: without `--postgres` it starts an embedded Postgres in `data/mac-dev-pg` (the
`pgserver` package), and it prints the prompts the scripted model knows, with the fixture sites' addresses. With
`SAMMY_TEST_MODEL` set (and its API key), a real model answers instead.

`scripts/build-app.sh https://sammy.example.com` builds an app that talks to that server; users can change it in
Settings. The app is signed ad hoc; for users, sign it with a Developer ID and notarize it (see the script).

## Test it

```bash
swift test                                                   # unit tests (wire formats, keys, Markdown, models)
SAMMY_TEST_SERVER=http://127.0.0.1:8000 swift test           # and the journeys, against the dev server
```

The journeys sign up a new person each and go through what a person does: a chat and its reply, a question, signing
in through the live view and approving an order, stopping, notifications from other chats, drafts, sign-out, an
ended session, schedules, attachments, and the races found in review (double sends, answers closed elsewhere, leaving a
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

## Telemetry

Once signed in, the app asks the server (`GET /api/telemetry`) whether to send traces; with `LOGFIRE_TOKEN` set
there, it sends them (OpenTelemetry, OTLP protobuf) to `/api/telemetry/v1/traces` through its own session, so the
cookie and site login go with them, and the server forwards them to Logfire with its token: the app holds none.
Otherwise, and when signed out, nothing is sent and spans are no-ops. `Telemetry.swift` has the rules.

- Every API request is a client span named for its route (`GET /api/threads/{thread_id}`) and carries
  `traceparent`, so the server's spans for it join the trace. The user's actions (send, answer, approve, deny, stop,
  rename, delete, open a chat or page) are spans with the requests under them; a takeover is one span for as long as
  it lasts (connections, reconnects, frames, how it ended); each run-events stream is one, with the run's statuses
  as events. Also: launch and sign-in, run status changes, notifications, errors shown, foreground and background.
  Polling (the chat list, the browser's picture) makes no spans.
- Never sent: passwords (account, site login, anything typed while taking over), cookies, `Authorization`,
  live-view links and hand-off ids (`/live/*`), push subscriptions, file names and paths, request and response
  bodies. URLs are route templates (no query); ids are the random UUIDs the server uses.
- What the user wrote and Sammy said (messages, replies, questions, answers, reasons, error messages) only when the
  server's `include_content` is on; otherwise their lengths and kinds.

## Design

Sammy looks like Logfire: its tokens (platform `src/services/logfire-frontend/src/styles/design-tokens.css`) are in
`Theme.swift` with their Logfire names. Neutral surfaces and hairline outlines; one control height (32, or 28 small)
and radius (8 containers, 6 controls); blue for anything you act on (a step deeper than Logfire's `--link`, so
white text on it passes 4.5:1); Pydantic pink only for the brand and the "needs you" dot; the system font, and mono
for metadata. Native where it matters: a real sidebar list, the window's own title, menus with shortcuts, a menu bar
menu, Notification Center and the Dock badge for chats that need you.

Decisions from review:

- While Sammy works or waits, Send becomes Stop, as in other chat apps; ⌘. stops too. A stopped or failed task can
  be tried again as it was asked (⌘R), or put back in the message box to change first. Approving has no keyboard
  shortcut: going ahead with something that costs money takes a click.
- What Sammy asks (a question, an approval, a hand-off) takes the message box's place, as in T3 Code: the user
  answers where they type, with Stop at hand. Typed answers survive the question closing, switching chats, and being
  answered elsewhere; a question can also be answered from its notification.
- While Sammy works, ↩ queues the next message: it goes when the task is done, or comes back to the box if the task
  failed or was stopped. ↑ in an empty box brings back the last task. "Working for 12s" ticks while Sammy works;
  "Worked for 1m 3s · 5 steps" folds its steps after.
- Deleting a chat asks nothing: it goes at once, with Undo (⌘Z) for a few seconds; the server deletes it after that,
  or straight away at sign-out or quitting. Back and Forward (⌘[ ⌘]) go through the chats and pages you visited.
- Taking over fills the window, and nothing (⌘N, a menu, a notification) takes the user away mid sign-in; ⇧⌘T leaves,
  ⌘↩ hands back. Typing goes through macOS text input, so accents and input methods work; ⌘V pastes from the Mac.
  With VoiceOver, the remote page reads like a web page: the server sends its outline (headings, text, fields,
  buttons, with where they are; passwords only as a count), and activating an item clicks it.
- Files go in the chat: drop them on it, paste them (⌘V: a screenshot, or files copied in Finder), or pick them with
  the paperclip. Each uploads at once, as a chip above the message box (a thumbnail for pictures), and goes with the
  next message; up to 10 a message, 20 MB each. Files on messages (yours, and those Sammy shares) open or save from
  the chat: pictures and PDFs open in Preview, anything else is saved where you choose.
- Watching Sammy's browser opens it beside the chat; it can fill the window (⇧⌘F, or double-click it), and from there
  the whole screen.
- Chats can be renamed and deleted from the chat's ⋯ menu, the File menu, the sidebar (right-click, swipe, ⌫) or
  ⌘⌫ there; failed and stopped chats are marked in the sidebar. Replies have a Copy button.
- The app opens where you left it, with what you were writing in each chat; signing out forgets both. A chat whose
  task finished while you looked elsewhere (or while the app was closed) is bold with a dot until you open it, and
  opening it clears its notification; each chat has at most one notification, its latest news.
- The sidebar is laid out as Codex's: the window's buttons alone at the top, New task as the first row, the chats,
  and the library pinned at the bottom. Hovering a chat shows a button to delete it.
- ⌘K opens a command palette (chats and actions, as T3 Code's); ⌘1…⌘9 go to the chats in the sidebar's order (the
  Go to Chat menu lists them), ⇧⌘1…5 to the library. A chat that needs you says what, in a word: Question, Approval
  or Browser. Chats can be marked unread.
- Reading further up a chat, it stays put while Sammy writes, with a button back to the latest. Sammy's browser
  beside the chat widens a narrow window to fit (or fills the window on a small screen) rather than squeeze both.
- Sammy's steps stay under its reply, folded; approvals and takeovers stay in the chat as one quiet line each.
- Sammy's squirrel (`SammySquirrel.swift`) sits by the composer and on the new-task screen, and reacts to the chat's
  mood: a nod when it starts working, the typing dots while it works, a `?` when it needs you, a hop and a check when
  it's done, and a slump when it fails. Small places (the toolbar, the sidebar, step rows) keep the prism, which reads
  at 9pt where a squirrel can't.

## Not yet

- Password reset emails a 6-digit code through the server's `SMTP_URL`; without one, no code is sent (set it for
  real users). Codes last 15 minutes and 5 tries.
- Without a Developer ID, the shared app is signed ad hoc (right-click > Open the first time).
- Notifications come from the app while it runs; "Open at login" (Settings) keeps it in the menu bar. Reaching a Mac
  where Sammy isn't running would need a push service (APNs).
- VoiceOver reads remote pages on Chromium (the live view's outline); Servo's live view can't read pages yet and says
  so. The outline covers what is on screen: scroll to reach the rest.
