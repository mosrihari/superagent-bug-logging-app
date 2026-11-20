"""The brain of a self-improving app: a Router, a Filter, Workers, and the state that ties them together.

Runs on the Claude Agent SDK, authenticated through the same `claude login` session as Claude Code on
this machine — so calls bill against your Claude subscription's monthly Agent SDK credit, not a separate
per-token API key. All three roles default to Haiku 5.5 (claude-haiku-5-5), the cheapest model, since
this is a prototype for testing. See README.md and docs/architecture.md.

The SDK runs its own agent loop and tool execution internally (that's the whole point of it), so unlike
a hand-rolled OpenAI-style chat-completions integration, this file never manually parses a tool_calls
list or feeds results back in a loop — `ClaudeSDKClient` does that. The Worker also gets Claude Code's
own Read/Write/Edit/Bash tools for free instead of us reimplementing file/shell access.

The Worker runs with permission_mode="bypassPermissions" (no per-action approval prompt) so it can act
without a human watching every step — that is what makes it a Super Agent and not a chat assistant. The
one safety net kept on top of that is `block_dangerous_bash`, a PreToolUse hook that refuses a short list
of command patterns (force-push, rm -rf near a root, piping a download into a shell, sudo, ...) before
they run. It is a net, not a sandbox: there is no container boundary around the Worker's shell. Keep
AGENT_REPO a throwaway repository and AGENT_GH_TOKEN scoped to just that repo. See docs/architecture.md.

One always-on process. It listens to Telegram (people), the Bug UI (bugs and enhancements filed
directly), Axiom (alerts on the product's own logs) and GitHub (pull request follow-ups). It decides
what is worth doing, dispatches a Worker per piece of work, and never merges its own pull request.

Run it anywhere that stays on: `uvicorn agent.brain:app --host 0.0.0.0 --port 8000`.

State is one JSON file (`agent/state.json` by default), guarded by a lock. That is enough for one
process; swap `State.save`/`State.__init__` for a real store before running more than one brain process.
"""
import asyncio
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import claude_agent_sdk as sdk
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------------------- settings
# Haiku 5.5: the cheapest Claude model, for all three roles while this is a prototype. Bump WORKER_MODEL
# to claude-sonnet-5-5 later if Haiku's fixes are too shallow for real bugs — see README.md.
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-5-5")
FILTER_MODEL = os.environ.get("FILTER_MODEL", "claude-haiku-5-5")
WORKER_MODEL = os.environ.get("WORKER_MODEL", "claude-haiku-5-5")
# A global cumulative-spend cap, checked before every model call, PLUS passed as each session's own
# max_budget_usd as a second, SDK-enforced backstop. Keep this well under your monthly Agent SDK credit.
SPEND_CAP = float(os.environ.get("SPEND_CAP", "5.0"))
CUT = 160        # characters of a trigger's text inlined before it is spilled behind a pointer
CORRELATE_WINDOW_S = 48 * 3600   # how long an open ticket stays eligible for a correlation match
CORRELATE_OVERLAP = 0.35         # minimum keyword-overlap ratio to call two reports the same problem

AGENT_REPO = os.environ.get("AGENT_REPO", "")                 # "you/demo-app" on GitHub — keep it a throwaway
AGENT_GH_TOKEN = os.environ.get("AGENT_GH_TOKEN", "")
GH_TEAM = set(filter(None, os.environ.get("GH_TEAM", "").split(",")))
BUG_UI_URL = os.environ.get("BUG_UI_URL", "").rstrip("/")     # e.g. https://your-bug-ui.vercel.app
BUG_UI_TOKEN = os.environ.get("BUG_UI_TOKEN", "")             # shared secret, brain <-> bug UI
AXIOM_WEBHOOK_TOKEN = os.environ.get("AXIOM_WEBHOOK_TOKEN", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_TELEGRAM = set(filter(None, os.environ.get("ALLOWED_TELEGRAM", "").split(",")))  # empty = anyone
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
STATE_PATH = Path(os.environ.get("STATE_PATH", Path(__file__).parent / "state.json"))
WORK_ROOT = Path(os.environ.get("WORK_ROOT", Path(__file__).parent / "work"))


def now():
    return round(time.time(), 2)


# ------------------------------------------------------------------------------- Worker safety net
# Not a sandbox — there is no container boundary around the Worker's shell. This only refuses a short
# list of command *patterns* before they run. See the module docstring and docs/architecture.md.
BLOCKED_BASH_PATTERNS = [re.compile(p, re.I) for p in [
    r"\brm\s+-rf\s+(/|~|\.\.)",            # delete from a root, home, or parent directory
    r"\bgit\s+push\b.*(--force|-f\b)",     # force-push
    r"\bgit\s+reset\s+--hard\s+origin",    # discard local history against upstream
    r"\bsudo\b",                            # privilege escalation
    r"\b(shutdown|reboot)\b",
    r"(curl|wget)[^|]*\|\s*(sh|bash)\b",   # pipe a download straight into a shell
    r"\.ssh/",                              # SSH keys/config
    r"\bchmod\s+777\s+/",
]]


async def block_dangerous_bash(input_data, tool_use_id, context):
    """A PreToolUse hook: refuses a Bash call matching a blocked pattern before it runs."""
    if input_data.get("tool_name") == "Bash":
        command = input_data.get("tool_input", {}).get("command", "")
        for pattern in BLOCKED_BASH_PATTERNS:
            if pattern.search(command):
                st.log("system", f"Blocked a Worker command matching a safety rule: {command[:160]}")
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": f"Blocked by this prototype's safety net (matches {pattern.pattern!r}). Try a narrower, more specific command."}}
    return {}


# ------------------------------------------------------------------------------------------ prompts
FILTER_POLICY = """You judge one message from a product's users or team for an always-on agent. Most chat is not work.
Classify it into exactly one of:
- "bug": a report that something is broken, wrong, slow or confusing in the product.
- "enhancement": a request for new or changed behaviour the product does not have yet.
- "chat": anything else (greetings, thanks, unrelated talk).
A bug or enhancement needs no explicit request; a plain report is worth acting on even when nobody asks.
Answer only JSON on one line: {"kind": "bug" | "enhancement" | "chat", "reason": "at most 8 words"}."""

ROUTER_SYSTEM = """You are the Router of a Super Agent that keeps one small product's bugs fixed and its
feature requests implemented. You are one long-lived session and its single voice to the team and to users.
You do NOT execute tasks. You have no shell and you never read code. You only: talk to people, dispatch
Workers, and track how tickets relate.
Rules:
- Every new, unmatched ticket gets one Worker: call start_worker with a clear task, its ticket_id (the Bug UI
  id given in the event) and the ticket's kind (bug or enhancement). Do not break one ticket into steps.
- Follow-ups on a ticket go to its Worker: attach(trigger, worker). Do not start a second Worker for a ticket
  that already has one; the event text will say so when a code-level correlation match already found it.
- A Worker shown merged=yes has finished: its pull request is merged. Never attach a new request to it; a new
  request after a merge is a new ticket and gets a new Worker (it starts from the merged code).
- An event marked REDIRECTED is new work a person already asked for: start its Worker and reply in one short
  line that you are on it, naming the ticket id. Never ask whether they want it as a ticket.
- A different problem is a new ticket and gets its own Worker, even when it touches the same files or comes
  while another Worker is busy.
- An event showing policy=silent in its EVENT line (an Axiom alert, not a person) gets policy "silent" on
  start_worker: start its Worker and say nothing in chat until it reports confirmed.
- A bug or enhancement reported directly by a person in Telegram: reply at once in one short line saying you
  are on it, then start the Worker.
- When a Worker reports: if status is not_confirmed, say nothing. Otherwise write the reply yourself, in the
  channel the ticket came from (use reply with the ticket's own thread id).
- Chat replies: under 100 words, conclusions only, ask only what needs a decision. Never paste logs.
- Long texts are cut short with a pointer; call read(id) to see the rest. Do not guess at what was cut.
- Say each thing once. If a Worker's report adds nothing new for people, say nothing and end your turn.
- When there is nothing to do, end your turn without calling a tool."""

WORKER_SYSTEM = """You are a Worker of a Super Agent. You have one ticket, your own context, and a clone of
the product's repository in your working directory. You never talk to people; you report to the Router, and
the Router replies for you. Your ticket's kind is either "bug" (reproduce, then fix) or "enhancement"
(implement, with tests).
You have Claude Code's own Read, Write, Edit and Bash tools for the repository itself. Beyond those, you
have: open_pr, push_changes, update_ticket (tell the Bug UI your ticket's current status), subscribe,
redirect, report.
Rules:
- Bug: reproduce with a real failing test before changing code. Tests: python -m pytest -q
- Enhancement: write the feature with a passing test; do not change behaviour that neither the ticket nor a
  person's follow-up on it asked for.
- Read every event in NEW EVENTS: several can arrive together, such as a merge notice and a new request.
- While your pull request is open, a person's follow-up asking for more or different changes is part of your
  ticket, even when it goes beyond the original task: implement it with a test, push_changes to the same pull
  request, and update_ticket with a note saying what you added. Never open a second pull request.
- Once your pull request is merged, your ticket is finished. A person's request that arrives after the merge,
  or together with the merge notice, is new work: call redirect(trigger, kind, reason) for it. Do not
  implement it yourself and do not tell anyone to file a ticket; redirect does that.
- Keep changes small. Open one pull request from a branch named agent/<short-name>, with a body in three
  parts: Problem, Change, Verification.
- Never run git checkout, git branch, git commit or git push yourself. Write the files, then call open_pr
  once: it makes the branch, commits and pushes. Your shell has no credentials to push.
- Call update_ticket whenever your ticket's status changes: "investigating" once you start, "fix_opened" (or
  "enhancement_opened") once you have a pull request, "done" only after it reports merged to you.
- On policy "silent": if you cannot confirm the problem with evidence, call update_ticket("not_confirmed", ...)
  and report not_confirmed. Do not change anything you cannot justify with evidence.
- If a reported error does not plausibly explain the symptom, say so in your report instead of guessing.
- When your pull request is merged, call update_ticket("done", ...) once, then report confirmed and stop.
- Report a final result once via report(status, summary). Status is confirmed, not_confirmed, progress, or
  question. Summary: at most 60 words, facts only."""


# -------------------------------------------------------------------------------------------- state
class State:
    """Everything the brain knows. Saved to STATE_PATH on every change."""

    def __init__(self):
        self.lock = threading.RLock()
        self.d = {"triggers": {}, "threads": {}, "workers": {}, "queue": [], "inbox": {}, "spill": {},
                  "log": [], "n": {"T": 0, "W": 0, "S": 0},
                  "counts": {"events": 0, "code_drops": 0, "model_judged": 0, "model_drops": 0,
                             "passed_through": 0, "correlated": 0, "router_calls": 0, "router_decisions": 0,
                             "replies": 0, "spend": 0.0, "worker_calls": 0, "restarts": 0},
                  "cursor": {"tg": 0}, "seen": {}, "started": now()}
        if STATE_PATH.exists():
            self.d = json.loads(STATE_PATH.read_text())
            self.d["counts"]["restarts"] += 1
            self.log("system", f"Brain restarted. Reloaded {len(self.d['triggers'])} triggers, {len(self.d['workers'])} workers from disk.")

    def save(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.d))

    def log(self, kind, text, **data):
        with self.lock:
            self.d["log"].append({"n": len(self.d["log"]), "t": now(), "kind": kind, "text": text, **data})
            self.save()
        print(f"[{kind}] {text}", flush=True)

    def next(self, k):
        self.d["n"][k] += 1
        return f"{k}{self.d['n'][k]}"

    def spill(self, text):
        sid = self.next("S")
        self.d["spill"][sid] = text
        return sid

    def spend(self, cost):
        """Adds a Claude call's real cost (ResultMessage.total_cost_usd) to the running total."""
        with self.lock:
            self.d["counts"]["spend"] += cost or 0.0
            self.save()

    def over_budget(self):
        return self.d["counts"]["spend"] > SPEND_CAP


st = State()


# ----------------------------------------------------------------------------------- Bug UI client
def bugui(method, path, body=None):
    """Talks to the Bug UI's API. The Bug UI is the system of record for every ticket."""
    if not BUG_UI_URL:
        return {}
    req = urllib.request.Request(
        BUG_UI_URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "x-bugui-token": BUG_UI_TOKEN})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def bugui_create(kind, title, description, source, reporter):
    return bugui("POST", "/api/tickets", {"type": kind, "title": title, "description": description,
                                          "source": source, "reporter": reporter, "fromBrain": True})


def bugui_update(ticket_id, **fields):
    return bugui("PATCH", f"/api/tickets/{ticket_id}", fields)


# -------------------------------------------------------------------------------------- GitHub client
def gh(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + AGENT_GH_TOKEN, "Accept": "application/vnd.github+json",
                 "User-Agent": "superagent-bug-logging-app", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


# --------------------------------------------------------------------------------- views for the Router
def one_line(t):
    w = t.get("text", "").replace("\n", " ")
    return f"[{t['id']}] {t['source']} {t['actor']}: \"{w[:70]}{'...' if len(w) > 70 else ''}\""


def inject_view(t):
    """Inline and Spill: the event's id and head, what is near it, and a pointer to the rest."""
    d = st.d
    text = t["text"]
    head = text if len(text) <= CUT else text[:CUT] + f"... [cut: rest -> read({t['id']})]"
    out = [f"EVENT [{t['id']}] {t['source']} {t['actor']} thread={t['thread']}"
           + (f" kind={t['kind']}" if t.get("kind") else "") + (f" policy={t['policy']}" if t.get("policy") else ""),
           f'  "{head}"']
    if t.get("redirected_from"):
        out.append(f"REDIRECTED by {t['redirected_from']}: new work after its pull request merged. Start a new Worker.")
    tr = d["threads"].get(t["thread"], {}).get("triggers", [])
    near = [x for x in tr if x != t["id"]][-3:]
    if near:
        out.append("NEAR IN SPACE (same Thread)")
        out += ["  " + one_line(d["triggers"][x]) for x in near]
    subs = d["threads"].get(t["thread"], {}).get("subscribers", [])
    out.append(f"THREAD [{t['thread']}] subscribers {subs}" + (f" ticket={t['ticket_id']}" if t.get("ticket_id") else ""))
    return "\n".join(out)


def router_view():
    d = st.d
    lines = [f"[{w['id']}] \"{w['task'][:70]}\" kind={w.get('kind','')} status={w['status']} ticket={w.get('ticket_id','')} threads={w['threads']}"
             + (" merged=yes" if w.get("merged") else "")
             for w in d["workers"].values()]
    return "OPEN AND PAST WORKERS\n" + ("\n".join(lines) or "none")


def router_read(i):
    d = st.d
    if i in d["triggers"]:
        t = d["triggers"][i]
        return f"{one_line(t)}\nfull text: {t['text']}\nthread [{t['thread']}]"
    if i in d["workers"]:
        w = d["workers"][i]
        return f"[{i}] task: {w['task']}\nkind: {w.get('kind','')}\nstatus: {w['status']}\nlast report: {w.get('summary','')}\nnext ids: {w['threads']}\npr: {w.get('pr','')}"
    if i in d["threads"]:
        th = d["threads"][i]
        return f"[{i}] subscribers {th['subscribers']}\n" + "\n".join(one_line(d["triggers"][x]) for x in th["triggers"][-8:])
    if i in d["spill"]:
        return d["spill"][i]
    return f"unknown id {i}"


# ------------------------------------------------------------------------------------- correlation
_WORD = re.compile(r"[a-z0-9]+")


def _words(text):
    return set(_WORD.findall(text.lower()))


def correlate(kind, actor, text, exclude=None):
    """Code-only dedup: does an open ticket of the same kind already cover this? A heuristic, not a
    guarantee — see docs/architecture.md. A match means pass-through, not a fresh model call.
    A merged Worker never matches: anything new after a merge is a new ticket."""
    d = st.d
    best = None
    words = _words(text)
    for w in d["workers"].values():
        if w.get("kind") != kind or w["status"] != "running" or w.get("merged") or w["id"] == exclude:
            continue
        if now() - w.get("started", 0) > CORRELATE_WINDOW_S:
            continue
        if w.get("reporter") == actor:
            return w["id"]
        overlap = len(words & _words(w.get("task", ""))) / max(1, len(words))
        if overlap >= CORRELATE_OVERLAP and (best is None or overlap > best[1]):
            best = (w["id"], overlap)
    return best[0] if best else None


# ------------------------------------------------------------------------------------- Claude Filter
async def classify_async(source, actor, text):
    """One stateless Haiku call: bug, enhancement, or chat. Billed against the subscription's Agent SDK
    credit, via the same `claude login` session as Claude Code on this machine."""
    options = sdk.ClaudeAgentOptions(model=FILTER_MODEL, system_prompt=FILTER_POLICY, allowed_tools=[],
                                     permission_mode="bypassPermissions", max_turns=1, max_budget_usd=0.02)
    content, cost = "", 0.0
    async for msg in sdk.query(prompt=f"{source} {actor}: {text}", options=options):
        if isinstance(msg, sdk.AssistantMessage):
            content += "".join(b.text for b in msg.content if isinstance(b, sdk.TextBlock))
        if isinstance(msg, sdk.ResultMessage):
            cost = msg.total_cost_usd or 0.0
    return content, cost


def classify(source, actor, text):
    try:
        content, cost = asyncio.run(classify_async(source, actor, text))
        st.spend(cost)
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else {"kind": "chat", "reason": "unclear"}
    except Exception as e:
        st.log("system", f"Filter call failed: {type(e).__name__}")
        return {"kind": "chat", "reason": "filter unavailable"}


# ------------------------------------------------------------------------------------------ intake
def intake(source, thread, actor, text, ext=None, kind=None, ticket_id=None, policy=None):
    """Filter, correlate, then route. Code decides what code can decide."""
    with st.lock:
        d = st.d
        if ext and ext in d["seen"]:
            return None
        if ext:
            d["seen"][ext] = 1
        tid = st.next("T")
        t = {"id": tid, "source": source, "thread": thread, "actor": actor, "text": text, "t": now(),
             "disposition": "", **({"kind": kind} if kind else {}), **({"ticket_id": ticket_id} if ticket_id else {}),
             **({"policy": policy} if policy else {})}
        d["triggers"][tid] = t
        th = d["threads"].setdefault(thread, {"id": thread, "subscribers": [], "triggers": []})
        th["triggers"].append(tid)
        d["counts"]["events"] += 1
        st.log("event", f"{source}: {actor} says \"{text[:110]}\"", id=tid)

        if actor.lower() in ("build-bot", "dependabot", "renovate"):
            t["disposition"] = "dropped"
            d["counts"]["code_drops"] += 1
            st.log("code", f"{actor} is on the ignore list. No model called.", id=tid)
            return tid
        if source == "telegram" and ALLOWED_TELEGRAM and actor not in ALLOWED_TELEGRAM:
            t["disposition"] = "dropped"
            d["counts"]["code_drops"] += 1
            st.log("code", f"{actor} is not on the Telegram allowlist. No model called.", id=tid)
            return tid
        if source == "github" and (text.startswith("CI ") or re.fullmatch(r"pull request #\d+ merged", text)) \
                and not th["subscribers"] and not any(w.get("pr", "").endswith(f"/pull/{thread[3:]}") for w in d["workers"].values()):
            t["disposition"] = "dropped"
            return tid
        merged = re.fullmatch(r"pull request #(\d+) merged", text) if source == "github" else None
        if merged:
            # Done before the pass-through below, so the merge notice still reaches the Worker (it marks
            # its ticket done) but a chat message right after it already takes the new-ticket path.
            mark_merged(merged.group(1))
        followups = []
        if th["subscribers"]:
            t["disposition"] = "passed"
            d["counts"]["passed_through"] += 1
            human = source in ("telegram", "bugui") or (source == "github" and actor in GH_TEAM)
            for w in th["subscribers"]:
                # A person's message on an ended Ticket's own Thread resumes it — same Mandate, no new
                # Eval, matching the reference design's rule that bots and CI do not wake ended Tickets
                # but a person's follow-up does. Without this, a message to a Worker that already
                # reported confirmed/not_confirmed just sits in its inbox forever: wake_worker refuses
                # anything whose status isn't "running".
                if human and d["workers"].get(w, {}).get("status") != "running":
                    d["workers"][w]["status"] = "running"
                    st.log("router", f"{w}'s ticket resumes: a person followed up after it ended.", worker=w)
                d["inbox"].setdefault(w, []).append(inject_view(t))
                # A person's follow-up while the PR is open is part of the same ticket: record it on the
                # board, whichever channel it came from, so the ticket shows everything that was asked.
                wt = d["workers"].get(w, {})
                # (A reply on the card itself is saved by the Bug UI already.)
                if human and source != "bugui" and wt.get("ticket_id") and not wt.get("merged"):
                    followups.append(wt["ticket_id"])
            st.log("pass", f"{th['subscribers'][0]} already follows this thread. No Router call.", id=tid, workers=th["subscribers"])
            st.save()
            subscribers = list(th["subscribers"])
        else:
            subscribers = None

    if subscribers is not None:
        for ticket in dict.fromkeys(followups):
            try:
                bugui_update(ticket, comment=f"Follow-up from {actor} via {source}: {text[:300]}")
            except Exception:
                pass
        for w in subscribers:
            wake_worker(w)
        return tid
    return route_new(tid, kind=kind, ticket_id=ticket_id)


def mark_merged(number):
    """A Worker whose pull request merged has finished its ticket. It keeps its own PR Thread, but leaves
    chat Threads — a Telegram chat (one Thread for every request in it) and its ticket's card on the board —
    so the next request there is judged as new work instead of being passed straight to a finished Worker."""
    with st.lock:
        d = st.d
        for w in d["workers"].values():
            if w.get("merged") or not w.get("pr", "").endswith(f"/pull/{number}"):
                continue
            w["merged"] = True
            for thread in [x for x in w["threads"] if x.startswith(("tg:", "bugui:"))]:
                w["threads"].remove(thread)
                subs = d["threads"].get(thread, {}).get("subscribers", [])
                if w["id"] in subs:
                    subs.remove(w["id"])
            st.log("code", f"{w['id']}'s pull request #{number} merged. New requests in its chat get a new ticket.", worker=w["id"])


def route_new(tid, kind=None, ticket_id=None, exclude=None):
    """A Trigger nobody follows yet: classify, correlate, else create a ticket and queue it for the Router.
    Also the path for a message a Worker redirected as new work (exclude= that Worker)."""
    with st.lock:
        t = st.d["triggers"][tid]
        source, thread, actor, text = t["source"], t["thread"], t["actor"], t["text"]
        th = st.d["threads"][thread]
    if kind is None:
        v = classify(source, actor, text)
        with st.lock:
            d = st.d
            d["counts"]["model_judged"] += 1
        if v.get("kind") == "chat":
            with st.lock:
                t["disposition"] = "dropped"
                st.d["counts"]["model_drops"] += 1
            st.log("model", f"Dropped as chat: {v.get('reason','')}.", id=tid)
            return tid
        kind = v["kind"]
        with st.lock:
            t["kind"] = kind

    match = correlate(kind, actor, text, exclude=exclude)
    if match:
        with st.lock:
            d = st.d
            th["subscribers"].append(match)
            d["workers"][match]["threads"].append(thread)
            d["inbox"].setdefault(match, []).append(inject_view(t))
            d["counts"]["correlated"] += 1
            t["disposition"] = "correlated"
            worker_ticket = d["workers"][match].get("ticket_id")
        st.log("code", f"Same {kind} as {match}'s ticket (keyword/reporter match). No new ticket, no Router call.", id=tid, worker=match)
        if ticket_id is None and worker_ticket:
            try:
                bugui_update(worker_ticket, comment=f"Another report of the same issue: {text[:200]}")
            except Exception:
                pass
        wake_worker(match)
        return tid

    # A ticket filed on the board arrives with its ticket_id; anything else new needs one, including a
    # reply on a merged ticket's card (source bugui, but new work).
    if ticket_id is None:
        try:
            created = bugui_create(kind, title=text[:80], description=text, source=source, reporter=actor)
            ticket_id = created.get("id")
            with st.lock:
                t["ticket_id"] = ticket_id
        except Exception as e:
            st.log("system", f"Could not create a Bug UI ticket: {type(e).__name__}")

    with st.lock:
        d = st.d
        t["disposition"] = "queued"
        d["queue"].append(tid)
    return tid


# ---------------------------------------------------------------------------- Router: business logic
# A Worker's session does not stay connected forever waiting for follow-ups — it does one round (the
# initial task, or a later batch of new inbox events) and disconnects. _busy_workers tracks which
# Worker ids currently have a round in progress, so wake_worker() below can start a fresh round when
# something new arrives for an idle one, instead of only reacting within a fixed window after it
# started. Without this, a PR comment arriving after a Worker's first round ended would sit in its
# inbox forever with nothing left running to read it — see docs/architecture.md.
_busy_workers = set()
_busy_lock = threading.Lock()


def _mark_busy(wid):
    with _busy_lock:
        if wid in _busy_workers:
            return False
        _busy_workers.add(wid)
        return True


def _mark_free(wid):
    with _busy_lock:
        _busy_workers.discard(wid)


def wake_worker(wid):
    """Starts a new round for wid if one is not already running. Safe to call every time something is
    appended to a Worker's inbox, including while it is already busy (a no-op then; its current round
    will pick up the new item when it next checks)."""
    w = st.d["workers"].get(wid)
    if not w or w.get("status") != "running":
        return
    if not _mark_busy(wid):
        return
    threading.Thread(target=_run_resume_round, args=(wid,), daemon=True).start()


def _run_resume_round(wid):
    try:
        asyncio.run(run_worker_round_async(wid))
    finally:
        _mark_free(wid)


def do_start_worker(task, kind, ticket_id, threads_csv, policy):
    with st.lock:
        d = st.d
        wid = st.next("W")
        threads = [t for t in threads_csv.split(",") if t]
        if ticket_id and f"bugui:{ticket_id}" not in threads:
            threads.append(f"bugui:{ticket_id}")   # replies on the ticket's own card reach its Worker
        w = {"id": wid, "task": task, "kind": kind, "ticket_id": ticket_id, "threads": threads,
             "policy": policy, "status": "running", "started": now(), "reporter": ""}
        for th in threads:
            tr = d["threads"].get(th, {}).get("triggers", [])
            if tr:
                w["reporter"] = d["triggers"][tr[-1]]["actor"]
        d["workers"][wid] = w
        for th in threads:
            d["threads"].setdefault(th, {"id": th, "subscribers": [], "triggers": []})["subscribers"].append(wid)
        d["inbox"][wid] = []
        d["counts"]["router_decisions"] += 1
        st.log("router", f"Router starts {wid} ({kind}, policy {policy}): {task[:120]}", worker=wid)
    context = "\n\n".join(inject_view(d["triggers"][d["threads"][th]["triggers"][-1]]) for th in threads if d["threads"].get(th, {}).get("triggers"))
    if ticket_id:
        try:
            bugui_update(ticket_id, status="investigating")
        except Exception:
            pass
    _mark_busy(wid)
    threading.Thread(target=run_worker, args=(wid, task, kind, policy, ticket_id, context), daemon=True).start()
    return f"started {wid}"


def do_attach(trigger_id, worker_id):
    with st.lock:
        d = st.d
        t = d["triggers"].get(trigger_id)
        if not t or worker_id not in d["workers"]:
            return "unknown trigger or worker"
        th = d["threads"][t["thread"]]
        if worker_id not in th["subscribers"]:
            th["subscribers"].append(worker_id)
            d["workers"][worker_id]["threads"].append(t["thread"])
        d["inbox"].setdefault(worker_id, []).append(inject_view(t))
        st.log("router", f"Router attaches {trigger_id} to {worker_id}.", worker=worker_id)
    wake_worker(worker_id)
    return "attached"


def send_reply(thread, text):
    d = st.d
    if thread.startswith("tg:") and len(text.split()) > 100:
        return "refused: chat replies must be under 100 words"
    with st.lock:
        d["counts"]["replies"] += 1
        st.log("reply", f"Router replies on {thread}: \"{text}\"", thread=thread)
    try:
        if thread.startswith("tg:") and TELEGRAM_TOKEN:
            telegram("sendMessage", chat_id=thread.split(":", 1)[1], text=text)
        elif thread.startswith("gh:") and AGENT_GH_TOKEN:
            gh("POST", f"/repos/{AGENT_REPO}/issues/{thread[3:]}/comments", {"body": text})
        elif thread.startswith("bugui:"):
            bugui_update(thread.split(":", 1)[1], comment=f"Agent: {text}")
    except Exception as e:
        st.log("system", f"Sending the reply failed: {type(e).__name__}")
    return "sent"


# ---------------------------------------------------------------------- Router: Claude Agent SDK tools
@sdk.tool("start_worker", "Start one Worker for one ticket.",
          {"task": str, "kind": str, "ticket_id": str, "threads": "comma-separated Thread ids", "policy": str})
async def tool_start_worker(args):
    text = do_start_worker(args.get("task", ""), args.get("kind", "bug"), args.get("ticket_id", ""),
                           args.get("threads", ""), args.get("policy", "normal"))
    return {"content": [{"type": "text", "text": text}]}


@sdk.tool("reply", "Say something to people, in the channel of a Thread id.", {"thread": str, "text": str})
async def tool_reply(args):
    return {"content": [{"type": "text", "text": send_reply(args["thread"], args["text"])}]}


@sdk.tool("attach", "Route a Trigger to the Worker that owns its ticket.", {"trigger": str, "worker": str})
async def tool_attach(args):
    return {"content": [{"type": "text", "text": do_attach(args["trigger"], args["worker"])}]}


@sdk.tool("message_worker", "Send a Worker an instruction.", {"worker": str, "text": str})
async def tool_message_worker(args):
    with st.lock:
        st.d["inbox"].setdefault(args["worker"], []).append("FROM ROUTER: " + args["text"])
    wake_worker(args["worker"])
    return {"content": [{"type": "text", "text": "sent"}]}


@sdk.tool("read", "Recall an object by id: a Trigger, a Thread or a Worker. The result names the next ids.", {"id": str})
async def tool_read(args):
    return {"content": [{"type": "text", "text": router_read(args["id"])}]}


ROUTER_ALLOWED = ["mcp__router__start_worker", "mcp__router__reply", "mcp__router__attach",
                  "mcp__router__message_worker", "mcp__router__read"]


# --------------------------------------------------------------------------------------- Router loop
async def router_loop_async():
    options = sdk.ClaudeAgentOptions(
        model=ROUTER_MODEL, system_prompt=ROUTER_SYSTEM, permission_mode="bypassPermissions",
        mcp_servers={"router": sdk.create_sdk_mcp_server(
            "router", tools=[tool_start_worker, tool_reply, tool_attach, tool_message_worker, tool_read])},
        allowed_tools=ROUTER_ALLOWED, max_turns=6, max_budget_usd=SPEND_CAP)
    async with sdk.ClaudeSDKClient(options=options) as client:
        while True:
            await asyncio.sleep(0.5)
            with st.lock:
                batch = st.d["queue"][:]
                st.d["queue"] = []
            if not batch:
                continue
            if st.over_budget():
                st.log("system", f"Spend cap (${SPEND_CAP}) reached. The Router will not call the model.")
                continue
            events = [x if x.startswith("REPORT") else inject_view(st.d["triggers"][x]) for x in batch]
            prompt = router_view() + "\n\nNEW\n" + "\n\n".join(events)
            try:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, sdk.ResultMessage):
                        st.spend(msg.total_cost_usd)
                        with st.lock:
                            st.d["counts"]["router_calls"] += 1
                            st.save()
            except Exception as e:
                st.log("system", f"A Router model call failed: {type(e).__name__}. It will retry on the next event.")


def router_loop():
    asyncio.run(router_loop_async())


# --------------------------------------------------------------------------------------- Telegram
def telegram(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def telegram_loop():
    if not TELEGRAM_TOKEN:
        return
    while True:
        try:
            r = telegram("getUpdates", offset=st.d["cursor"]["tg"] + 1, timeout=25)
            for u in r.get("result", []):
                with st.lock:
                    st.d["cursor"]["tg"] = u["update_id"]
                    st.save()
                m = u.get("message") or {}
                if not m.get("text") or (m.get("from") or {}).get("is_bot") or m["text"].startswith("/"):
                    continue
                who = (m.get("from") or {}).get("username") or (m.get("from") or {}).get("first_name", "someone")
                # message_id is only unique within one chat, not globally — scope the dedup key by chat
                # too, or two different chats' first messages (both id 1) would collide.
                intake("telegram", f"tg:{m['chat']['id']}", who, m["text"], ext=f"tg{m['chat']['id']}:{m['message_id']}")
        except Exception as e:
            st.log("system", f"Telegram poll failed: {type(e).__name__}: {e}")
            time.sleep(3)


# ----------------------------------------------------------------------------------------- GitHub
def github_loop():
    if not AGENT_GH_TOKEN or not AGENT_REPO:
        return
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))
    first = not st.d["seen"]

    def see(thread, actor, text, ext):
        if first:
            st.d["seen"][ext] = 1
        else:
            intake("github", thread, actor, text, ext=ext)

    while True:
        time.sleep(10)
        try:
            for c in gh("GET", f"/repos/{AGENT_REPO}/issues/comments?since={since}&per_page=30"):
                login = (c.get("user") or {}).get("login", "")
                if login not in GH_TEAM and not login.endswith("[bot]"):
                    continue
                num = c["issue_url"].rsplit("/", 1)[1]
                see(f"gh:{num}", login, c.get("body") or "", f"c{c['id']}")
            # Inline review comments (commenting on a specific diff line) are a separate GitHub
            # endpoint from the conversation comments above — without this, a Worker never sees them.
            for c in gh("GET", f"/repos/{AGENT_REPO}/pulls/comments?since={since}&per_page=30"):
                login = (c.get("user") or {}).get("login", "")
                if login not in GH_TEAM and not login.endswith("[bot]"):
                    continue
                num = c["pull_request_url"].rsplit("/", 1)[1]
                path_line = f" ({c['path']}:{c.get('line') or c.get('original_line')})" if c.get("path") else ""
                see(f"gh:{num}", login, (c.get("body") or "") + path_line, f"rc{c['id']}")
            for pr in gh("GET", f"/repos/{AGENT_REPO}/pulls?state=all&sort=updated&direction=desc&per_page=10"):
                num, sha = pr["number"], pr["head"]["sha"]
                if pr.get("merged_at"):
                    see(f"gh:{num}", "a teammate", f"pull request #{num} merged", f"m{num}")
                for cr in gh("GET", f"/repos/{AGENT_REPO}/commits/{sha}/check-runs").get("check_runs", []):
                    if cr.get("status") == "completed":
                        see(f"gh:{num}", cr.get("name", "ci"), f"CI {cr.get('conclusion')} on #{num} at {sha[:7]}", f"ci{cr['id']}")
            first = False
        except Exception:
            pass


# ------------------------------------------------------------------------------------------ Worker
def brain_call(op, **k):
    """A Worker's own thread calling back into this same process's state, in-process instead of over HTTP."""
    with st.lock:
        d = st.d
        if op == "inbox":
            items = d["inbox"].get(k["wid"], [])
            d["inbox"][k["wid"]] = []
            return items
        if op == "pr":
            w = d["workers"][k["wid"]]
            w["pr"] = k["url"]
            th = d["threads"].setdefault(f"gh:{k['number']}", {"id": f"gh:{k['number']}", "subscribers": [], "triggers": []})
            if k["wid"] not in th["subscribers"]:
                th["subscribers"].append(k["wid"])
                w.setdefault("threads", []).append(f"gh:{k['number']}")
            st.log("worker", f"{k['wid']} opened pull request #{k['number']}.", worker=k["wid"], url=k["url"])
            return None
        if op == "subscribe":
            th = d["threads"].setdefault(k["thread"], {"id": k["thread"], "subscribers": [], "triggers": []})
            w = d["workers"][k["wid"]]
            if k["wid"] not in th["subscribers"]:
                th["subscribers"].append(k["wid"])
                w.setdefault("threads", []).append(k["thread"])
            return None
        if op == "redirect":
            t = d["triggers"].get(k["trigger"])
            if not t:
                return f"unknown trigger {k['trigger']}"
            if t.get("redirected_from"):
                return "already redirected"
            w = d["workers"][k["wid"]]
            # Leave a shared chat Thread so the chat's next messages follow the new ticket; a PR Thread
            # stays this Worker's own.
            if not t["thread"].startswith("gh:"):
                subs = d["threads"].get(t["thread"], {}).get("subscribers", [])
                if k["wid"] in subs:
                    subs.remove(k["wid"])
                if t["thread"] in w["threads"]:
                    w["threads"].remove(t["thread"])
            t["redirected_from"] = k["wid"]
            st.log("worker", f"{k['wid']} redirects {k['trigger']} as new work: {k['reason']}", worker=k["wid"], id=k["trigger"])
            return "ok"
        if op == "report":
            w = d["workers"][k["wid"]]
            w["status"] = {"confirmed": "done", "not_confirmed": "discarded"}.get(k["status"], "running")
            w["summary"] = k["summary"]
            d["queue"].append(f"REPORT from {k['wid']} (ticket {w.get('ticket_id','')}, kind {w.get('kind','')}): status={k['status']}. {k['summary']}" + (f" PR: {w.get('pr')}" if w.get("pr") else ""))
            st.log("worker", f"{k['wid']} reports ({k['status']}): {k['summary']}", worker=k["wid"])
            return None
    return None


def build_worker_session(wid, ticket_id, repo, kind, policy):
    """Builds one round's tools and ClaudeAgentOptions. Called fresh for the initial round and for
    every later resume round — a round is a new connection each time, not one held-open session, so
    this closure (and the `done` flag it shares with the `report` tool) is rebuilt each time too."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    done = {"v": False}

    @sdk.tool("open_pr", "Commit all changes, push and open a pull request.", {"branch": str, "title": str, "body": str})
    async def open_pr(args):
        subprocess.run(["git", "checkout", "-b", args["branch"]], cwd=repo, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo)
        subprocess.run(["git", "commit", "-m", args["title"]], cwd=repo, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", args["branch"]], cwd=repo, env=env, capture_output=True)
        pr = gh("POST", f"/repos/{AGENT_REPO}/pulls", {"title": args["title"], "body": args["body"], "head": args["branch"], "base": "main"})
        if "number" not in pr:
            return {"content": [{"type": "text", "text": f"open_pr failed: {pr}"}], "is_error": True}
        brain_call("pr", wid=wid, number=pr["number"], url=pr["html_url"])
        if ticket_id:
            try:  # the board's "View pull request" link reads pr_url; a Bug UI outage must not fail the PR
                bugui_update(ticket_id, pr_url=pr["html_url"])
            except Exception as e:
                st.log("system", f"{wid}: could not record PR link on {ticket_id}: {type(e).__name__}", worker=wid)
        return {"content": [{"type": "text", "text": f"opened pull request #{pr['number']}: {pr['html_url']}"}]}

    @sdk.tool("push_changes", "Commit all changes and push them to the branch of your open pull request.", {"message": str})
    async def push_changes(args):
        subprocess.run(["git", "add", "-A"], cwd=repo)
        subprocess.run(["git", "commit", "-m", args["message"]], cwd=repo, capture_output=True)
        subprocess.run(["git", "push"], cwd=repo, env=env, capture_output=True)
        return {"content": [{"type": "text", "text": "pushed"}]}

    @sdk.tool("update_ticket", "Tell the Bug UI this ticket's current status.", {"status": str, "note": str})
    async def update_ticket(args):
        if ticket_id:
            try:
                bugui_update(ticket_id, status=args["status"], comment=args.get("note", ""))
            except Exception as e:
                return {"content": [{"type": "text", "text": f"update_ticket failed: {type(e).__name__}"}], "is_error": True}
        return {"content": [{"type": "text", "text": "ok"}]}

    @sdk.tool("subscribe", "Follow another Thread: its new events reach you directly.", {"thread": str})
    async def subscribe(args):
        brain_call("subscribe", wid=wid, thread=args["thread"])
        return {"content": [{"type": "text", "text": "subscribed"}]}

    @sdk.tool("redirect", "After your pull request has merged: hand a person's new request (a Trigger id) to a new "
              "ticket and Worker. kind is bug or enhancement.", {"trigger": str, "kind": str, "reason": str})
    async def redirect(args):
        r = brain_call("redirect", wid=wid, trigger=args["trigger"], reason=args.get("reason", ""))
        if r != "ok":
            return {"content": [{"type": "text", "text": f"redirect failed: {r}"}], "is_error": True}
        kind_ = args.get("kind") if args.get("kind") in ("bug", "enhancement") else None
        # route_new may call the Filter model and the Bug UI; run it off this session's event loop.
        threading.Thread(target=route_new, args=(args["trigger"],), kwargs={"kind": kind_, "exclude": wid}, daemon=True).start()
        return {"content": [{"type": "text", "text": "redirected: it becomes a new ticket with its own Worker"}]}

    @sdk.tool("report", "Report to the Router.", {"status": str, "summary": str})
    async def report(args):
        brain_call("report", wid=wid, status=args["status"], summary=args["summary"])
        done["v"] = True
        return {"content": [{"type": "text", "text": "ok"}]}

    policy_line = "\nPolicy for this ticket: silent. Do not report or change anything without evidence." if policy == "silent" else ""
    options = sdk.ClaudeAgentOptions(
        model=WORKER_MODEL, system_prompt=WORKER_SYSTEM + policy_line, cwd=str(repo),
        permission_mode="bypassPermissions", max_turns=40, max_budget_usd=SPEND_CAP,
        hooks={"PreToolUse": [sdk.HookMatcher(matcher="Bash", hooks=[block_dangerous_bash])]},
        mcp_servers={"worker": sdk.create_sdk_mcp_server(
            "worker", tools=[open_pr, push_changes, update_ticket, subscribe, redirect, report])},
        allowed_tools=["Read", "Write", "Edit", "Bash", "mcp__worker__open_pr", "mcp__worker__push_changes",
                       "mcp__worker__update_ticket", "mcp__worker__subscribe", "mcp__worker__redirect",
                       "mcp__worker__report"])
    return options, done


async def _drain_inbox(client, wid, done):
    """Keeps feeding a still-connected client any inbox items that arrive while it's connected, so a
    burst of near-together events reuses one round instead of opening a new one for each. Stops (and
    lets the caller disconnect) as soon as the inbox is empty — it does not wait or poll; wake_worker
    is what starts the next round whenever something new shows up after that."""
    for _ in range(20):
        if done["v"] or st.over_budget():
            return
        inbox = brain_call("inbox", wid=wid)
        if not inbox:
            return
        await client.query("NEW EVENTS\n" + "\n\n".join(inbox))
        async for msg in client.receive_response():
            if isinstance(msg, sdk.ResultMessage):
                st.spend(msg.total_cost_usd)


async def run_worker_async(wid, task, kind, policy, ticket_id, context):
    """The initial round: clone the repo, then work the ticket. Marked busy by do_start_worker before
    this is spawned; run_worker's finally frees it once this round disconnects."""
    repo = WORK_ROOT / wid
    repo.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        subprocess.run(["git", "clone", "-q", f"https://x-access-token:{AGENT_GH_TOKEN}@github.com/{AGENT_REPO}.git", str(repo)],
                       check=True, env=env, capture_output=True, timeout=120)
        for a, b in (("user.name", "Super Agent"), ("user.email", "superagent@example.com")):
            subprocess.run(["git", "config", a, b], cwd=repo)
    except Exception as e:
        brain_call("report", wid=wid, status="not_confirmed", summary=f"Could not prepare the repository: {type(e).__name__}")
        return

    options, done = build_worker_session(wid, ticket_id, repo, kind, policy)
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(f"TICKET kind={kind}\nTASK: {task}\n\n{context}")
        async for msg in client.receive_response():
            if isinstance(msg, sdk.ResultMessage):
                st.spend(msg.total_cost_usd)
        await _drain_inbox(client, wid, done)


async def run_worker_round_async(wid):
    """A resume round: something new arrived for a Worker that had already gone idle. Reconnects —
    a fresh session, not a continued conversation, so the prompt tells it to check its directory, git
    log and any open pull request before repeating earlier work, the same caution a restarted Assignment
    gets in the reference design this is built from."""
    w = st.d["workers"].get(wid)
    if not w or w.get("status") != "running":
        return
    inbox = brain_call("inbox", wid=wid)
    if not inbox:
        return
    repo = WORK_ROOT / wid
    options, done = build_worker_session(wid, w.get("ticket_id", ""), repo, w.get("kind", "bug"), w.get("policy", "normal"))
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(
            f"You are resuming this ticket in a new session, not the one that did the earlier work.\n"
            f"TICKET kind={w.get('kind','bug')}\nTASK: {w.get('task','')}\n\n"
            "Check your directory, git log and any open pull request before repeating anything that "
            "may already be done.\n\nNEW EVENTS\n" + "\n\n".join(inbox))
        async for msg in client.receive_response():
            if isinstance(msg, sdk.ResultMessage):
                st.spend(msg.total_cost_usd)
        await _drain_inbox(client, wid, done)


def run_worker(wid, task, kind, policy, ticket_id, context):
    try:
        asyncio.run(run_worker_async(wid, task, kind, policy, ticket_id, context))
    finally:
        _mark_free(wid)


# ------------------------------------------------------------------------------------------- API
app = FastAPI(title="Superagent bug logging app — brain")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.on_event("startup")
def start():
    # Workers started before do_start_worker added it don't follow their own card's Thread yet.
    for wid, w in st.d["workers"].items():
        tid_ = w.get("ticket_id")
        if tid_ and f"bugui:{tid_}" not in w["threads"] and not w.get("merged"):
            w["threads"].append(f"bugui:{tid_}")
            subs = st.d["threads"].setdefault(f"bugui:{tid_}", {"id": f"bugui:{tid_}", "subscribers": [], "triggers": []})["subscribers"]
            if wid not in subs:
                subs.append(wid)
    # Merges seen before mark_merged existed (or before a crash) still count: replay them from history.
    for t in list(st.d["triggers"].values()):
        m = re.fullmatch(r"pull request #(\d+) merged", t["text"]) if t["source"] == "github" else None
        if m:
            mark_merged(m.group(1))
    for f in (router_loop, telegram_loop, github_loop):
        threading.Thread(target=f, daemon=True).start()
    # A restart kills every in-progress round outright, with no clean shutdown — including one that
    # was never waiting on a new event at all (it could have been mid-task, with real uncommitted
    # changes on disk). So every "running" Worker gets resumed here, not just ones with something
    # already queued: if its inbox is empty, give it a recovery note first, the same caution a
    # restarted Assignment gets in the reference design — check the directory, git log and any open
    # PR before deciding what, if anything, still needs doing.
    #
    # A Worker that had already ended (confirmed/not_confirmed) can still have messages stuck in its
    # inbox from before intake()'s resume-on-follow-up logic existed, or from a Worker that ended
    # between a message being queued and the next restart. Those are resumed here too — the one-time
    # cost of also resuming a stray bot/CI message queued the same way is acceptable; the alternative
    # is a person's real follow-up sitting there forever.
    for wid, w in st.d["workers"].items():
        has_inbox = bool(st.d["inbox"].get(wid))
        if w.get("status") != "running":
            if not has_inbox:
                continue
            w["status"] = "running"
            st.log("router", f"{wid}'s ticket resumes at startup: a queued message predates when it could be resumed.", worker=wid)
        if not has_inbox:
            st.d["inbox"].setdefault(wid, []).append(
                "RECOVERY: the brain restarted and may have interrupted you mid-task, with no chance "
                "to report. Check your directory (git status/diff), git log, and whether a pull "
                "request is already open before deciding what, if anything, still needs doing.")
        wake_worker(wid)
    st.save()
    st.log("system", f"The brain is running on {ROUTER_MODEL} (Router/Filter) and {WORKER_MODEL} (Worker), spend cap ${SPEND_CAP}.")


def auth(x, expected):
    if expected and x != expected:
        raise HTTPException(403)


@app.post("/webhooks/bugui")
async def webhook_bugui(req: Request, x_bugui_token: str = Header("")):
    """A ticket was filed directly in the Bug UI. Already typed; no classification needed."""
    auth(x_bugui_token, BUG_UI_TOKEN)
    b = await req.json()
    threading.Thread(target=intake, args=("bugui", f"bugui:{b['id']}", b.get("reporter", "someone"), b["description"]),
                     kwargs={"kind": b["type"], "ticket_id": b["id"], "ext": f"bugui{b['id']}"}, daemon=True).start()
    return {"ok": True}


@app.post("/webhooks/bugui/followup")
async def webhook_bugui_followup(req: Request, x_bugui_token: str = Header("")):
    """A person replied on a ticket's card. Its Thread is bugui:<id>: while the ticket's PR is open its Worker
    follows that Thread and gets it; after the merge nobody does, so it is judged as new work like any chat
    message. The Bug UI has already saved it as a comment on the card."""
    auth(x_bugui_token, BUG_UI_TOKEN)
    b = await req.json()
    if not b.get("id") or not (b.get("text") or "").strip():
        raise HTTPException(400)
    threading.Thread(target=intake, args=("bugui", f"bugui:{b['id']}", b.get("reporter") or "someone", b["text"].strip()),
                     daemon=True).start()
    return {"ok": True}


@app.post("/webhooks/axiom")
async def webhook_axiom(req: Request, x_axiom_token: str = Header("")):
    """Axiom's saved-search alert fired. Always a bug, always silent (see FILTER_POLICY and WORKER_SYSTEM)."""
    auth(x_axiom_token, AXIOM_WEBHOOK_TOKEN)
    b = await req.json()
    signature = b.get("signature") or b.get("title") or "unknown error"
    text = f"Axiom alert: {signature}. {b.get('description', '')}"
    threading.Thread(target=intake, args=("axiom", f"axiom:{signature}", "axiom", text),
                     kwargs={"kind": "bug", "ext": b.get("id"), "policy": "silent"}, daemon=True).start()
    return {"ok": True}


@app.get("/events")
def events(after: int = -1, x_admin: str = Header("")):
    auth(x_admin, ADMIN_SECRET)
    d = st.d
    return {"log": d["log"][after + 1:], "counts": d["counts"], "workers": list(d["workers"].values())}


@app.get("/health")
def health():
    return {"ok": True, "started": st.d["started"]}
