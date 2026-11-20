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
           │          │                              └──► PATCHes the Bug UI ticket's status
GitHub ────┘          │                                        ▲
  PR comments ────────┴──────── pass-through / wake_worker() ──┘  (no Router call for an owned thread)
```

Three listeners, proven working end to end: **Bug UI**, **Telegram**, **GitHub**. (Axiom was designed in and the webhook endpoint exists — see "Axiom, parked" below — but it isn't part of the live loop right now.)

## The three pieces

| Piece | What it is | Where it runs |
|---|---|---|
| `agent/brain.py` | The Router, the Filter, the Worker, the state. One file, read top to bottom, same ethos as the reference `build-a-super-agent` project. Runs on the Claude Agent SDK, authenticated via this machine's `claude login` session. | Any always-on host: Modal, Fly.io, Render, a VM. **Not Vercel** — it needs a long-lived background loop. |
| `bug-ui/` | The ticket board: bugs and enhancements, one list, filed by a person or by the brain. System of record for every ticket, regardless of which Source reported it. | Vercel (or any Next.js host) |
| `demo-app/` | The small product that evolves. A tiny Flask app with real bugs and room for features, with structured logs. | Pushed to its own throwaway GitHub repo; the Worker clones and works it there. Not deployed as a live service in this build. |

## Sources and how each reaches the brain

| Source | Mechanism | What it produces |
|---|---|---|
| Telegram | long-polling (`getUpdates`), same as the reference project | A raw message. The Filter classifies it: `bug`, `enhancement`, or `chat` (dropped). |
| Bug UI | webhook (`POST /webhooks/bugui`) fired when a person files a ticket directly in the UI | An already-typed ticket (`bug` or `enhancement`) — no classification needed |
| GitHub | polling (`gh` REST API) for both conversation comments *and* inline review comments, plus CI and merge status | Either a pass-through to the Worker that owns the PR's thread (no Router call), or, if nothing owns it, a fresh candidate |

**Why Bug UI is already typed, but Telegram isn't:** a person filing a ticket in the UI already tells you what kind of work it is. A Telegram message is just words — that's the one place the two-layer Filter (code rules, then a small model reading `FILTER_POLICY`) earns its keep.

## A Worker doesn't stay connected forever

The first real bug this build hit: a Worker's session used to poll its own inbox for a *fixed* window after its first report, then disconnect for good — a PR comment arriving after that point queued forever with nothing left running to read it. The fix is `wake_worker(wid)` in `brain.py`: every place that appends to a Worker's inbox (a GitHub pass-through, `attach`, `message_worker`, a correlation match) calls it, and it starts a fresh round only if one isn't already in progress for that Worker (tracked in `_busy_workers`). A resume round is a new Claude session, not a continued conversation — it's told to check its directory, git log and any open PR before repeating prior work, same caution the reference design gives a restarted Assignment. A startup sweep also wakes any Worker left with a non-empty inbox, so a restart doesn't strand a queued comment either.

## Axiom, parked

The `/webhooks/axiom` endpoint and its Filter/Router policy text still exist in `brain.py` — verified against Axiom's real Custom Webhook Notifier template variables and secret-header support, not guessed. It isn't live because doing that for real needs two more things this build doesn't have yet: `demo-app` actually deployed and receiving traffic (right now it's only ever run via `pytest` inside a Worker's own clone), and the brain reachable from the public internet (a tunnel or a real deployment, not `localhost`). Revisit this once those exist.

## One ticket, wherever it was filed

The Bug UI is the single system of record. Whichever Source first reports an issue, it gets **one** ticket there with `type: bug | enhancement`, `status`, and a `source` field. The brain's own internal Ticket keeps the Bug UI ticket id as Source-owned metadata (same principle as `dsh-superagent-prototype`'s rule: *"a Source keeps nothing on a Ticket beyond what it needs; what it keeps about a Trigger is in its meta"*) — the brain's core logic never needs to know a ticket's Bug UI id to make its own decisions.

## Correlation, before the Router ever gets involved

Before creating a second ticket for the same problem, `intake()` runs a cheap, code-only correlation check against currently open tickets of the same type: same reporter, or enough keyword overlap, within a time window. A match means **pass-through** (fan-out to the existing Ticket's Worker, exactly like a GitHub PR comment) — no Eval, no new Worker, no model call. This is deliberately a heuristic, not a guarantee: see the conversation's discussion of what happens when a user's complaint and an Axiom alert *don't* actually match — the Worker, not the correlation heuristic, is responsible for deciding plausibility and reporting honestly instead of guessing.

## Policy, per Source

- **Telegram:** `ALLOWED_TELEGRAM` is an allowlist of names; empty means anyone can report a bug or request a feature (decide this deliberately — see `README.md`).
- **Bug UI:** a ticket filed through the UI is always admitted — it's the most explicit kind of request there is.
- **GitHub:** only logins in `GH_TEAM` are admitted as people; everything else is read-only context.
- *(Axiom, when it's live: a bot-sourced, inferred signal, acted on silently — no chat reply, straight to a Worker — via the trigger's structural `policy=silent` field, not inference from source text alone.)*

## What ends a Worker's turn

Same rule as both reference projects: **a Worker never merges its own pull request.** It opens the PR, pushes updates when a reviewer comments, and updates the Bug UI ticket's status (`investigating → fix_opened → in_review → done`). A human merges. That's the one gate that doesn't move, no matter which Source started the Ticket.

## What's stubbed, deliberately, in this reference build

Same spirit as both repos it's built from: this is a working skeleton to run and extend, not a production system.

- State is one JSON file on disk, guarded by a lock — fine for one process, not for scaling the brain horizontally. Swap for Modal's `Dict`, Redis, or Postgres before that matters.
- The Bug UI's local dev storage is SQLite, which **does not survive Vercel's serverless filesystem** — swap `bug-ui/lib/db.ts` for a real hosted Postgres (Neon, Vercel Postgres) before deploying it there. This is called out again in `bug-ui/README` section.
- Correlation is keyword-overlap, not embeddings or a real search index.
- There's no auto-merge path; add one deliberately (tiny diff, passing CI, explicit opt-in) if you want it.
