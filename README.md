# Superagent Bug Logging App

A small, self-improving app: users report bugs or ask for features in Telegram or a ticket board,
the product's own errors reach it through Axiom, and one always-on Router dispatches an ephemeral
Worker per ticket, which fixes the bug or implements the feature and opens a pull request. A human
only ever merges it.

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

You need Python 3.11+, Node 18+, an [OpenRouter](https://openrouter.ai) key, and a GitHub account.
Telegram and Axiom are both optional for a first run.

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
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in OPENROUTER_API_KEY, AGENT_REPO, AGENT_GH_TOKEN, BUG_UI_TOKEN
export $(grep -v '^#' ../.env | xargs)
uvicorn brain:app --reload --port 8000
```

**4. Try it without Telegram or Axiom first:**

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
- **Axiom:** ship `demo-app`'s logs there (`AXIOM_TOKEN`/`AXIOM_DATASET` in its own `.env`), write a
  saved query that aggregates errors (never raw log lines — see `docs/architecture.md`), and point its
  alert's webhook action at `POST <your-brain-url>/webhooks/axiom` with header `x-axiom-token` matching
  `AXIOM_WEBHOOK_TOKEN`.
- **GitHub:** already wired — PR comments and CI results on `AGENT_REPO` reach the Worker that opened
  the PR automatically, no extra setup.

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
  model-driven Worker run shell commands against a real repository. Keep `AGENT_REPO` a throwaway repo,
  scope its token to just that repo, and set a spending limit on your OpenRouter key.
- **State is a single JSON file** (`agent/state.json`), guarded by one lock — fine for one process,
  not for scaling the brain horizontally.
- **Correlation across duplicate reports is a keyword-overlap heuristic**, not a guarantee. A Worker is
  still responsible for verifying a bug actually explains a symptom before "fixing" it — see
  `docs/architecture.md`'s section on correlation.
- **No auto-merge path exists.** Add one deliberately (tiny diff, green CI, explicit opt-in) if you want it.

## License

MIT.
