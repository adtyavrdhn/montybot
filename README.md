# montybot

Notes and design for an always-on agent built on Monty and Pydantic AI: reachable from chat channels, running in
Monty by default and in a real machine only when a process has to run, with computer use and human takeover.

## Design

[`DESIGN.md`](DESIGN.md) is the current design for monty-bot: scheduled browser runs in Monty and a sandboxed
Chromium, DBOS for schedules, and hand-off to the user when the agent gets stuck.

## Notes

Dated files in [`notes/`](notes/), newest last. Later notes win over earlier ones.

- [2026-10-06 clai2 vs Muse, Dots and Grok Bot](<notes/2026-10-06 clai2 vs Muse, Dots and Grok Bot.md>): what Meta
  Muse, OpenAI dots and xAI Grok Bot do, where clai2 stands, and designs for channels, Monty-first execution,
  E2B computer use, and sessions and takeover.
