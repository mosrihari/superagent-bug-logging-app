# Architecture

A Super Agent (DeepSeek's design: one always-on Router, ephemeral Workers it opens
itself, no human ever manages a session) applied to a concrete, useful job:
**keep one small app's bugs fixed and its feature requests implemented, with no
human triaging tickets.**

```
Telegram ──┐
           │ (bug, enhancement, or chat?)
Bug UI  ───┼──► Brain: Router (always-on) ──► Worker (per Ticket) ──► GitHub PR ──► human merges
           │          │                              │
Axiom   ───┘          │                              └──► PATCHes the Bug UI ticket's status
                       │                                        ▲
                       └──────────── GitHub PR comments ────────┘  (pass-through, no Router call)
```

## The four pieces

| Piece | What it is | Where it runs |
|---|---|---|
| `agent/brain.py` | The Router, the Filter, the Worker, the state. One file, read top to bottom, same ethos as the reference `build-a-super-agent` project. | Any always-on host: Modal, Fly.io, Render, a VM. **Not Vercel** — it needs a long-lived background loop. |
| `bug-ui/` | The ticket board: bugs and enhancements, one list, filed by a person or by the brain. System of record for every ticket, regardless of which Source reported it. | Vercel (or any Next.js host) |
| `demo-app/` | The small product that evolves. A tiny Flask app with real bugs and room for features, with structured logs. | Vercel, or anywhere; its logs ship to Axiom |
| Axiom | Log search + alerting. Free tier; see `README.md`. Fires a webhook at the brain when an error pattern matches. | Axiom's cloud, or swap for Grafana Cloud/Loki |

## Sources and how each reaches the brain

| Source | Mechanism | What it produces |
|---|---|---|
| Telegram | long-polling (`getUpdates`), same as the reference project | A raw message. The Filter classifies it: `bug`, `enhancement`, or `chat` (dropped). |
| Bug UI | webhook (`POST /webhooks/bugui`) fired when a person files a ticket directly in the UI | An already-typed ticket (`bug` or `enhancement`) — no classification needed |
| Axiom | webhook (`POST /webhooks/axiom`) fired by a saved search's alert action | A `bug`-typed candidate, with the error signature already aggregated by Axiom's query (never raw log lines) |
| GitHub | polling (`gh` REST API) for PR comments, reviews and CI, same as the reference project | Either a pass-through to the Worker that owns the PR's thread (no Router call), or, if nothing owns it, a fresh candidate |

**Why Bug UI and Axiom are already typed, but Telegram isn't:** a person filing a ticket in the UI, or an alert firing on a known error pattern, already tells you what kind of work it is. A Telegram message is just words — that's the one place the two-layer Filter (code rules, then a small model reading `FILTER_POLICY`) earns its keep.

## One ticket, wherever it was filed

The Bug UI is the single system of record. Whichever Source first reports an issue, it gets **one** ticket there with `type: bug | enhancement`, `status`, and a `source` field. The brain's own internal Ticket keeps the Bug UI ticket id as Source-owned metadata (same principle as `dsh-superagent-prototype`'s rule: *"a Source keeps nothing on a Ticket beyond what it needs; what it keeps about a Trigger is in its meta"*) — the brain's core logic never needs to know a ticket's Bug UI id to make its own decisions.

## Correlation, before the Router ever gets involved

Before creating a second ticket for the same problem, `intake()` runs a cheap, code-only correlation check against currently open tickets of the same type: same reporter, or enough keyword overlap, within a time window. A match means **pass-through** (fan-out to the existing Ticket's Worker, exactly like a GitHub PR comment) — no Eval, no new Worker, no model call. This is deliberately a heuristic, not a guarantee: see the conversation's discussion of what happens when a user's complaint and an Axiom alert *don't* actually match — the Worker, not the correlation heuristic, is responsible for deciding plausibility and reporting honestly instead of guessing.

## Policy, per Source

- **Telegram:** `ALLOWED_TELEGRAM` is an allowlist of names; empty means anyone can report a bug or request a feature (decide this deliberately — see `README.md`).
- **Bug UI:** a ticket filed through the UI is always admitted — it's the most explicit kind of request there is.
- **Axiom:** an alert is a bot-sourced, inferred signal. It's only acted on silently (no chat reply, straight to a Worker) — the written analogue of `FILTER_POLICY`'s "silently reproduce and fix" rule.
- **GitHub:** only logins in `GH_TEAM` are admitted as people; everything else is read-only context.

## What ends a Worker's turn

Same rule as both reference projects: **a Worker never merges its own pull request.** It opens the PR, pushes updates when a reviewer comments, and updates the Bug UI ticket's status (`investigating → fix_opened → in_review → done`). A human merges. That's the one gate that doesn't move, no matter which Source started the Ticket.

## What's stubbed, deliberately, in this reference build

Same spirit as both repos it's built from: this is a working skeleton to run and extend, not a production system.

- State is one JSON file on disk, guarded by a lock — fine for one process, not for scaling the brain horizontally. Swap for Modal's `Dict`, Redis, or Postgres before that matters.
- The Bug UI's local dev storage is SQLite, which **does not survive Vercel's serverless filesystem** — swap `bug-ui/lib/db.ts` for a real hosted Postgres (Neon, Vercel Postgres) before deploying it there. This is called out again in `bug-ui/README` section.
- Correlation is keyword-overlap, not embeddings or a real search index.
- There's no auto-merge path; add one deliberately (tiny diff, passing CI, explicit opt-in) if you want it.
