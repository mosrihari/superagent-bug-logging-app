# Superagent Bug Logging App

A small, self-improving app: users report bugs or ask for features in Telegram or a ticket board, and
one always-on Router dispatches an ephemeral Worker per ticket, which fixes the bug or implements the
feature and opens a pull request. A human only ever merges it. Three listeners, proven working end to
end: **Bug UI**, **Telegram**, **GitHub** (PR comments and CI). Axiom was designed in and its webhook
endpoint exists, but isn't live yet — see `docs/architecture.md`.

Built from the same design as [`build-a-super-agent`](https://github.com/VizuaraAI/build-a-super-agent)
(DeepSeek's *Exploring Super-agent Design*) and [`dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype),
retargeted at a concrete, useful job instead of a webinar demo. Read **[docs/architecture.md](docs/architecture.md)**
first — it explains every decision below.

## What's here

```
agent/        the brain: Router, Filter, Workers, state (agent/brain.py — read it top to bottom)
bug-ui/       the ticket board: bugs and enhancements, one list, Next.js
demo-app/     the small product the agent evolves, with one planted bug and room for features
docs/         the architecture this implements
```

## Run it locally

You need Python 3.11+, Node 18+, a GitHub account, and a machine where you've already run
`claude login` (Claude Code's own login) — the brain authenticates through that session and bills
against your Claude subscription's monthly Agent SDK credit, no separate API key needed. Telegram is
optional for a first run; try `drive.py` against the Bug UI and GitHub first.

The Worker runs with `permission_mode="bypassPermissions"` — it edits files and runs shell commands
without pausing for approval, the thing that makes it a Super Agent instead of a chat assistant. There
is no container around it, only a short blocked-commands safety net (`block_dangerous_bash` in
`agent/brain.py`). Keep `AGENT_REPO` a disposable repository you don't mind an autonomous agent pushing
to, and watch the first several runs rather than leaving it unattended. See `docs/architecture.md`.

**1. The demo app.** Push it to its own repository and turn on GitHub Actions.

```bash
cd demo-app
pip install -r requirements.txt
python -m pytest -q        # 2 pass; nothing covers the planted bug yet
git init -b main && git add -A && git commit -m "Demo app"
gh repo create superagent-demo-app --private --source . --push
```

Make a [fine-grained GitHub token](https://github.com/settings/personal-access-tokens/new) scoped to
that one repo, with read/write on contents, pull requests and issues.

**2. The Bug UI.**

```bash
cd bug-ui
npm install
cp .env.example .env.local   # fill in BUG_UI_TOKEN; leave BRAIN_URL pointing at localhost:8000
npm run dev                  # http://localhost:3000
```

**3. The brain.**

```bash
cd agent
python -m venv venv && venv/Scripts/activate   # or `source venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in AGENT_REPO, AGENT_GH_TOKEN, BUG_UI_TOKEN
export $(grep -v '^#' ../.env | xargs)
uvicorn brain:app --reload --port 8000
```

**4. Try it without Telegram first:**

```bash
python agent/drive.py bug "Priya" "login fails after an idle session, shows a plain 401"
python agent/drive.py show
```

Open `http://localhost:3000` — you should see a `BUG-1` card move from *Open* to *Investigating* to
*Fix opened* as the Worker reproduces the bug, writes a failing test, fixes it, and opens a real pull
request on your `demo-app` repository. A human merges it; nothing here does that automatically.

## Wiring up the real Sources

- **Telegram:** make a bot with [@BotFather](https://t.me/BotFather), set `TELEGRAM_TOKEN`. Leave
  `ALLOWED_TELEGRAM` empty to let anyone message it, or list usernames to restrict it — decide this
  deliberately, it's a real authorization choice, not a default to skip past.
- **GitHub:** already wired — PR conversation comments, inline review comments, and CI results on
  `AGENT_REPO` all reach the Worker that opened the PR automatically, no extra setup.
- **Axiom (parked, not live):** the `/webhooks/axiom` endpoint exists and its body template is verified
  against Axiom's real Custom Webhook Notifier variables (see `docs/architecture.md`), but actually
  using it needs `demo-app` deployed somewhere receiving real traffic, and the brain reachable from the
  public internet (not `localhost`) — neither is set up in this build.

## Deploying

- **The brain** needs to stay running between events — it is not a good fit for Vercel's serverless
  functions. Deploy it to [Modal](https://modal.com), [Fly.io](https://fly.io), [Render](https://render.com),
  or any small always-on VM, running `uvicorn agent.brain:app`.
- **The Bug UI and the demo app** are both plain Next.js/Flask apps and deploy to Vercel normally —
  **except** `bug-ui/lib/db.ts` uses local SQLite, which does not survive Vercel's serverless filesystem.
  Swap it for a hosted Postgres (Neon or Vercel Postgres) before deploying the Bug UI for real; the five
  functions it exports (`listTickets`, `getTicket`, `createTicket`, `updateTicket`) are the only thing
  that needs to change.

## What this is not

Same caveats as the two projects it's built from:

- **Security is not solved.** This listens to untrusted input (chat messages, log alerts) and lets a
  model-driven Worker run shell commands against a real repository with no approval gate and no
  container boundary — only the blocked-commands hook in `agent/brain.py`. Keep `AGENT_REPO` a throwaway
  repo, scope its token to just that repo, and keep `SPEND_CAP` well under your monthly Agent SDK credit.
- **State is a single JSON file** (`agent/state.json`), guarded by one lock — fine for one process,
  not for scaling the brain horizontally.
- **Correlation across duplicate reports is a keyword-overlap heuristic**, not a guarantee. A Worker is
  still responsible for verifying a bug actually explains a symptom before "fixing" it — see
  `docs/architecture.md`'s section on correlation.
- **No auto-merge path exists.** Add one deliberately (tiny diff, green CI, explicit opt-in) if you want it.

## License

MIT.
