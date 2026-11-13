"""The brain of a self-improving app: a Router, a Filter, Workers, and the state that ties them together.

One always-on process. It listens to Telegram (people), the Bug UI (bugs and enhancements filed
directly), Axiom (alerts on the product's own logs) and GitHub (pull request follow-ups). It decides
what is worth doing, dispatches a Worker per piece of work, and never merges its own pull request.

Run it anywhere that stays on: `uvicorn agent.brain:app --host 0.0.0.0 --port 8000`.
See docs/architecture.md for the design this implements, and README.md for setup.

State is one JSON file (`agent/state.json` by default), guarded by a lock. That is enough for one
process; swap `State.save`/`State.__init__` for a real store (Redis, Postgres, Modal's Dict) before
running more than one brain process or caring about concurrent writers.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------------------- settings
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
FILTER_MODEL = os.environ.get("FILTER_MODEL", "deepseek/deepseek-v4.1-flash")
WORKER_MODEL = os.environ.get("WORKER_MODEL", "deepseek/deepseek-v4-pro")
SPEND_CAP = float(os.environ.get("SPEND_CAP", "15.0"))
CUT = 160        # characters of a trigger's text inlined before it is spilled behind a pointer
OUT_CUT = 1500   # characters of a worker's tool output inlined before it is spilled
CORRELATE_WINDOW_S = 48 * 3600   # how long an open ticket stays eligible for a correlation match
CORRELATE_OVERLAP = 0.35         # minimum keyword-overlap ratio to call two reports the same problem

AGENT_REPO = os.environ.get("AGENT_REPO", "")                 # "you/demo-app" on GitHub
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


def llm(model, messages, tools=None, max_tokens=1200, think=True):
    """One call through OpenRouter. Returns (message, cost, usage)."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
    if not think:
        body["reasoning"] = {"enabled": False}
    if tools:
        body["tools"] = tools
    for attempt in range(4):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                     "Content-Type": "application/json", "X-Title": "Superagent bug logging app"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            if "choices" in d:
                return d["choices"][0]["message"], float((d.get("usage") or {}).get("cost") or 0), d.get("usage") or {}
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("model call failed")


def tool(name, desc, **props):
    req = [k for k, v in props.items() if not v.get("optional")]
    clean = {k: {x: y for x, y in v.items() if x != "optional"} for k, v in props.items()}
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": clean, "required": req}}}


S = {"type": "string"}


def opt(**k):
    return {"type": "string", "optional": True, **k}


# ------------------------------------------------------------------------------------------ prompts
FILTER_POLICY = """You judge one message from a product's users or team for an always-on agent. Most chat is not work.
Classify it into exactly one of:
- "bug": a report that something is broken, wrong, slow or confusing in the product.
- "enhancement": a request for new or changed behaviour the product does not have yet.
- "chat": anything else (greetings, thanks, unrelated talk, a question already answered in your reply history).
A bug or enhancement needs no explicit request; a plain report is worth acting on even when nobody asks.
Answer only JSON: {"kind": "bug" | "enhancement" | "chat", "reason": "at most 8 words"}."""

ROUTER_SYSTEM = """You are the Router of a Super Agent that keeps one small product's bugs fixed and its
feature requests implemented. You are one long-lived session and its single voice to the team and to users.
You do NOT execute tasks. You have no shell and you never read code. You only: talk to people, dispatch
Workers, and track how tickets relate.
Rules:
- Every new, unmatched ticket gets one Worker: call start_worker with a clear task, its ticket_id (the Bug UI
  id given in the event) and the ticket's kind (bug or enhancement). Do not break one ticket into steps.
- Follow-ups on a ticket go to its Worker: attach(trigger, worker). Do not start a second Worker for a ticket
  that already has one; the event text will say so when a code-level correlation match already found it.
- A different problem is a new ticket and gets its own Worker, even when it touches the same files or comes
  while another Worker is busy.
- A bug ticket from Axiom (an alert, not a person) gets policy "silent": start its Worker and say nothing in
  chat until it reports confirmed.
- A bug or enhancement reported directly by a person in Telegram: reply at once in one short line saying you
  are on it, then start the Worker.
- When a Worker reports: if status is not_confirmed, say nothing. Otherwise write the reply yourself, in the
  channel the ticket came from (use reply with the ticket's own thread id).
- Chat replies: under 100 words, conclusions only, ask only what needs a decision. Never paste logs.
- Older work is spilled out of your window. To recall it, follow IDs with read(id); each result names the
  next IDs. Do not guess.
- Say each thing once. If a Worker's report adds nothing new for people, call done() without replying.
- When there is nothing to do, call done()."""

WORKER_SYSTEM = """You are a Worker of a Super Agent. You have one ticket, your own context, and a clone of
the product's repository at /work/repo. You never talk to people; you report to the Router, and the Router
replies for you. Your ticket's kind is either "bug" (reproduce, then fix) or "enhancement" (implement, with
tests).
Tools: list_files, read_file, write_file, start (runs a command in the background and returns at once), stop,
wait (until a background command finishes or a new event arrives), read (fetch a spilled item by id), open_pr,
push_changes, subscribe, update_ticket (tell the Bug UI your ticket's current status), report.
Rules:
- Long commands never block you. New events can arrive while a command runs; handle them as soon as they do.
- Bug: reproduce with a real failing test before changing code. Tests: python -m pytest -q
- Enhancement: write the feature with a passing test; do not change behaviour the ticket did not ask for.
- Keep changes small. Open one pull request from a branch named agent/<short-name>, with a body in three
  parts: Problem, Change, Verification.
- Never run git checkout, git branch, git commit or git push yourself. Write the files, then call open_pr
  once: it makes the branch, commits and pushes. Your shell has no credentials to push.
- Call update_ticket whenever your ticket's status changes: "investigating" once you start, "fix_opened" (or
  "enhancement_opened") once you have a pull request, "done" only after it reports merged to you.
- On policy "silent": if you cannot confirm the problem with evidence, call update_ticket("not_confirmed", ...)
  and report not_confirmed. Do not change anything you cannot justify with evidence.
- If a Splunk/Axiom error does not plausibly explain the reported symptom, say so in your report instead of
  guessing; ask the Router a question if you need one fact from the reporter before you can proceed.
- When your pull request is merged, call update_ticket("done", ...) once, then call wait.
- Report a final result once. After that, a passing CI run or other good news needs no new report: call wait.
- Finish with report(status, summary). Status is confirmed, not_confirmed, progress, or question. Summary: at
  most 60 words, facts only."""

ROUTER_TOOLS = [
    tool("start_worker", "Start one Worker for one ticket.", task=S, kind={"type": "string", "enum": ["bug", "enhancement"]},
         ticket_id=S, threads={"type": "array", "items": S}, policy={"type": "string", "enum": ["normal", "silent"]}),
    tool("reply", "Say something to people, in the channel of a Thread id.", thread=S, text=S),
    tool("attach", "Route a Trigger to the Worker that owns its ticket.", trigger=S, worker=S),
    tool("message_worker", "Send a Worker an instruction.", worker=S, text=S),
    tool("read", "Recall an object by id: a Trigger, a Thread or a Worker. The result names the next ids.", id=S),
    tool("done", "Nothing more to do this turn."),
]
WORKER_TOOLS = [
    tool("list_files", "List the repository's files."),
    tool("read_file", "Read a file.", path=S),
    tool("write_file", "Replace a file's contents.", path=S, content=S),
    tool("start", "Start a command in the background. Returns a job id at once.", command=S),
    tool("stop", "Stop a background job.", job=S),
    tool("wait", "Wait until a background job finishes or a new event arrives."),
    tool("read", "Read a spilled item by id.", id=S),
    tool("open_pr", "Commit all changes, push and open a pull request.", branch=S, title=S, body=S),
    tool("push_changes", "Commit all changes and push them to the branch of your open pull request.", message=S),
    tool("subscribe", "Follow another Thread: its new events reach you directly.", thread=S),
    tool("update_ticket", "Tell the Bug UI this ticket's current status.",
         status={"type": "string", "enum": ["investigating", "fix_opened", "enhancement_opened", "in_review", "done", "not_confirmed"]},
         note=S),
    tool("report", "Report to the Router.", status={"type": "string", "enum": ["confirmed", "not_confirmed", "progress", "question"]}, summary=S),
]


# -------------------------------------------------------------------------------------------- state
class State:
    """Everything the brain knows. Saved to STATE_PATH on every change."""

    def __init__(self):
        self.lock = threading.RLock()
        self.d = {"triggers": {}, "threads": {}, "workers": {}, "router": [], "queue": [], "feed": [],
                  "inbox": {}, "spill": {}, "log": [], "device": {}, "n": {"T": 0, "W": 0, "S": 0},
                  "counts": {"events": 0, "code_drops": 0, "model_judged": 0, "model_drops": 0,
                             "passed_through": 0, "correlated": 0, "router_calls": 0, "router_decisions": 0,
                             "replies": 0, "spend": 0.0, "tokens": 0, "worker_calls": 0, "restarts": 0},
                  "cursor": {"tg": 0, "gh": ""}, "seen": {}, "started": now()}
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
    """Creates the one ticket a bug or enhancement is filed under, regardless of which Source reported it."""
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
    """Inline and Spill: the event's id and head, what is near it, and pointers to the rest."""
    d = st.d
    text = t["text"]
    head = text if len(text) <= CUT else text[:CUT] + f"... [cut: rest -> read({t['id']})]"
    out = [f"EVENT [{t['id']}] {t['source']} {t['actor']} thread={t['thread']}" + (f" kind={t['kind']}" if t.get("kind") else ""),
           f'  "{head}"']
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
             for w in d["workers"].values()]
    feed = d["feed"][-8:]
    d["feed"] = []
    return "OPEN AND PAST WORKERS\n" + ("\n".join(lines) or "none") + \
        ("\nPASSED THROUGH SINCE LAST TURN (no decision needed)\n" + "\n".join(feed) if feed else "")


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


def correlate(kind, actor, text):
    """Code-only dedup: does an open ticket of the same kind already cover this? A heuristic, not a
    guarantee — see docs/architecture.md. A match means pass-through, not a fresh Eval."""
    d = st.d
    best = None
    words = _words(text)
    for w in d["workers"].values():
        if w.get("kind") != kind or w["status"] != "running":
            continue
        if now() - w.get("started", 0) > CORRELATE_WINDOW_S:
            continue
        if w.get("reporter") == actor:
            return w["id"]
        overlap = len(words & _words(w.get("task", ""))) / max(1, len(words))
        if overlap >= CORRELATE_OVERLAP and (best is None or overlap > best[1]):
            best = (w["id"], overlap)
    return best[0] if best else None


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
             "disposition": "", **({"kind": kind} if kind else {}), **({"ticket_id": ticket_id} if ticket_id else {})}
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
        if source == "github" and text.startswith("CI ") and not th["subscribers"]:
            t["disposition"] = "dropped"
            return tid
        if th["subscribers"]:
            t["disposition"] = "passed"
            d["counts"]["passed_through"] += 1
            for w in th["subscribers"]:
                d["inbox"].setdefault(w, []).append(inject_view(t))
            st.log("pass", f"{th['subscribers'][0]} already follows this thread. No Router call.", id=tid, workers=th["subscribers"])
            st.save()
            return tid

    # already typed (Bug UI, Axiom) skips the slow path; a raw Telegram message needs classifying
    if kind is None:
        try:
            msg, cost, use = llm(FILTER_MODEL, [{"role": "system", "content": FILTER_POLICY},
                                                {"role": "user", "content": f"{source} {actor}: {text}"}], max_tokens=120, think=False)
            m = re.search(r"\{.*\}", msg.get("content") or "", re.S)
            v = json.loads(m.group(0)) if m else {"kind": "chat", "reason": "unclear"}
        except Exception:
            v, cost, use = {"kind": "chat", "reason": "filter unavailable"}, 0, {}
        with st.lock:
            d = st.d
            d["counts"]["model_judged"] += 1
            d["counts"]["spend"] += cost
            d["counts"]["tokens"] += use.get("total_tokens", 0)
        if v["kind"] == "chat":
            with st.lock:
                t["disposition"] = "dropped"
                st.d["counts"]["model_drops"] += 1
            st.log("model", f"Dropped as chat: {v.get('reason','')}.", id=tid)
            return tid
        kind = v["kind"]
        with st.lock:
            t["kind"] = kind

    match = correlate(kind, actor, text)
    if match:
        with st.lock:
            d = st.d
            th["subscribers"].append(match)
            d["workers"][match]["threads"].append(thread)
            d["inbox"].setdefault(match, []).append(inject_view(t))
            d["counts"]["correlated"] += 1
            t["disposition"] = "correlated"
        st.log("code", f"Same {kind} as {match}'s ticket (keyword/reporter match). No new ticket, no Router call.", id=tid, worker=match)
        if ticket_id is None:
            bugui_update(d["workers"][match].get("ticket_id"), comment=f"Another report of the same issue: {text[:200]}")
        return tid

    if ticket_id is None and source != "bugui":
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


# ---------------------------------------------------------------------------------------- router
def router_tool(name, a):
    d = st.d
    if name == "start_worker":
        with st.lock:
            wid = st.next("W")
            w = {"id": wid, "task": a.get("task", ""), "kind": a.get("kind", "bug"), "ticket_id": a.get("ticket_id", ""),
                 "threads": a.get("threads", []), "policy": a.get("policy", "normal"), "status": "running",
                 "started": now(), "reporter": ""}
            for th in w["threads"]:
                tr = d["threads"].get(th, {}).get("triggers", [])
                if tr:
                    w["reporter"] = d["triggers"][tr[-1]]["actor"]
            d["workers"][wid] = w
            for th in w["threads"]:
                d["threads"].setdefault(th, {"id": th, "subscribers": [], "triggers": []})["subscribers"].append(wid)
            d["inbox"][wid] = []
            d["counts"]["router_decisions"] += 1
            st.log("router", f"Router starts {wid} ({w['kind']}, policy {w['policy']}): {w['task'][:120]}", worker=wid)
        context = "\n\n".join(inject_view(d["triggers"][d["threads"][th]["triggers"][-1]]) for th in w["threads"] if d["threads"].get(th, {}).get("triggers"))
        if w["ticket_id"]:
            try:
                bugui_update(w["ticket_id"], status="investigating")
            except Exception:
                pass
        threading.Thread(target=run_worker, args=(wid, w["task"], w["kind"], w["policy"], w["ticket_id"], context), daemon=True).start()
        return f"started {wid}"
    if name == "reply":
        return send_reply(a.get("thread", ""), a.get("text", ""))
    if name == "attach":
        with st.lock:
            t, wid = d["triggers"].get(a.get("trigger")), a.get("worker")
            if not t or wid not in d["workers"]:
                return "unknown trigger or worker"
            th = d["threads"][t["thread"]]
            if wid not in th["subscribers"]:
                th["subscribers"].append(wid)
                d["workers"][wid]["threads"].append(t["thread"])
            d["inbox"].setdefault(wid, []).append(inject_view(t))
            st.log("router", f"Router attaches {t['id']} to {wid}.", worker=wid)
        return "attached"
    if name == "message_worker":
        with st.lock:
            d["inbox"].setdefault(a.get("worker"), []).append("FROM ROUTER: " + a.get("text", ""))
        return "sent"
    if name == "read":
        return router_read(a.get("id", ""))
    return "ok"


def send_reply(thread, text):
    d = st.d
    if thread.startswith("tg:") and len(text.split()) > 100:
        return "refused: chat replies must be under 100 words"
    with st.lock:
        d["counts"]["replies"] += 1
        st.log("reply", f"Router replies on {thread}: \"{text}\"", thread=thread)
    try:
        if thread.startswith("tg:") and TELEGRAM_TOKEN:
            chat_id = thread.split(":", 1)[1]
            telegram("sendMessage", chat_id=chat_id, text=text)
        elif thread.startswith("gh:") and AGENT_GH_TOKEN:
            gh("POST", f"/repos/{AGENT_REPO}/issues/{thread[3:]}/comments", {"body": text})
    except Exception as e:
        st.log("system", f"Sending the reply failed: {type(e).__name__}")
    return "sent"


def router_loop():
    while True:
        time.sleep(0.5)
        d = st.d
        with st.lock:
            batch = d["queue"][:]
            d["queue"] = []
            if not batch:
                continue
            events = [x if x.startswith("REPORT") else inject_view(d["triggers"][x]) for x in batch]
            if d["counts"]["spend"] > SPEND_CAP:
                st.log("system", "Spend cap reached. The Router will not call the model.")
                continue
            d["router"].append({"role": "user", "content": router_view() + "\n\nNEW\n" + "\n\n".join(events)})
        for _ in range(6):
            try:
                msg, cost, use = llm(ROUTER_MODEL, [{"role": "system", "content": ROUTER_SYSTEM}] + d["router"][-24:], ROUTER_TOOLS, 700, think=False)
            except Exception:
                st.log("system", "A Router model call failed. It will retry on the next event.")
                break
            with st.lock:
                d["counts"]["router_calls"] += 1
                d["counts"]["spend"] += cost
                d["counts"]["tokens"] += use.get("total_tokens", 0)
                m = {"role": "assistant", "content": msg.get("content") or ""}
                if msg.get("tool_calls"):
                    m["tool_calls"] = msg["tool_calls"]
                d["router"].append(m)
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            finished = False
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                res = router_tool(fn, args)
                with st.lock:
                    d["router"].append({"role": "tool", "tool_call_id": c["id"], "content": str(res)})
                finished = finished or fn == "done"
            with st.lock:
                st.save()
            if finished:
                break


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
                st.d["cursor"]["tg"] = u["update_id"]
                m = u.get("message") or {}
                if not m.get("text") or (m.get("from") or {}).get("is_bot") or m["text"].startswith("/"):
                    continue
                who = (m.get("from") or {}).get("username") or (m.get("from") or {}).get("first_name", "someone")
                thread = f"tg:{m['chat']['id']}"
                intake("telegram", thread, who, m["text"], ext=f"tg{m['message_id']}")
        except Exception:
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
    """A Worker thread calling back into this same process's state, in-process instead of over HTTP."""
    with st.lock:
        d = st.d
        w = d["workers"].get(k.get("wid"), {})
        if op == "inbox":
            items = d["inbox"].get(k["wid"], [])
            d["inbox"][k["wid"]] = []
            return items
        if op == "log":
            d["counts"]["spend"] += k.get("cost", 0)
            d["counts"]["tokens"] += k.get("tokens", 0)
            d["counts"]["worker_calls"] += 1
            if k.get("text"):
                st.log("worker", f"{k['wid']}: {k['text']}", worker=k["wid"])
            return None
        if op == "spill":
            return st.spill(k["text"])
        if op == "subscribe":
            th = d["threads"].setdefault(k["thread"], {"id": k["thread"], "subscribers": [], "triggers": []})
            if k["wid"] not in th["subscribers"]:
                th["subscribers"].append(k["wid"])
                w.setdefault("threads", []).append(k["thread"])
            return None
        if op == "pr":
            w["pr"] = k["url"]
            th = d["threads"].setdefault(f"gh:{k['number']}", {"id": f"gh:{k['number']}", "subscribers": [], "triggers": []})
            if k["wid"] not in th["subscribers"]:
                th["subscribers"].append(k["wid"])
                w.setdefault("threads", []).append(f"gh:{k['number']}")
            st.log("worker", f"{k['wid']} opened pull request #{k['number']}.", worker=k["wid"], url=k["url"])
            return None
        if op == "report":
            w["status"] = {"confirmed": "done", "not_confirmed": "discarded"}.get(k["status"], "running")
            w["summary"] = k["summary"]
            d["queue"].append(f"REPORT from {k['wid']} (ticket {w.get('ticket_id','')}, kind {w.get('kind','')}): status={k['status']}. {k['summary']}" + (f" PR: {w.get('pr')}" if w.get("pr") else ""))
            st.log("worker", f"{k['wid']} reports ({k['status']}): {k['summary']}", worker=k["wid"])
            return None
    return None


def run_worker(wid, task, kind, policy, ticket_id, context):
    """One container's worth of work, in a thread here. Clones the repo, fixes or implements, opens a PR."""
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

    jobs = {}

    def spill_cut(text):
        if len(text) <= OUT_CUT:
            return text
        sid = brain_call("spill", wid=wid, text=text)
        return text[:OUT_CUT] + f"\n... [cut, {len(text)} characters: rest -> read({sid})]"

    def worker_tool(name, a):
        if name == "list_files":
            return "\n".join(str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
        if name == "read_file":
            try:
                return (repo / a["path"]).read_text()[:OUT_CUT]
            except Exception as e:
                return f"error: {type(e).__name__}"
        if name == "write_file":
            p = repo / a["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"])
            brain_call("log", wid=wid, text=f"Edits {a['path']}")
            return "written"
        if name == "start":
            jid = f"J{len(jobs) + 1}"
            jobs[jid] = subprocess.Popen(a["command"], shell=True, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            return jid
        if name == "stop":
            p = jobs.get(a["job"])
            if p:
                p.terminate()
            return "stopped"
        if name == "wait":
            for jid, p in list(jobs.items()):
                if p.poll() is not None:
                    out, _ = p.communicate()
                    del jobs[jid]
                    return f"{jid} finished, exit {p.returncode}: {spill_cut(out or '')}"
            time.sleep(2)
            return "no background job finished; new events (if any) are in your inbox"
        if name == "read":
            return router_read(a["id"])
        if name == "open_pr":
            subprocess.run(["git", "checkout", "-b", a["branch"]], cwd=repo, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=repo)
            subprocess.run(["git", "commit", "-m", a["title"]], cwd=repo, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", a["branch"]], cwd=repo, env=env, capture_output=True)
            pr = gh("POST", f"/repos/{AGENT_REPO}/pulls", {"title": a["title"], "body": a["body"], "head": a["branch"], "base": "main"})
            if "number" not in pr:
                return f"open_pr failed: {pr}"
            brain_call("pr", wid=wid, number=pr["number"], url=pr["html_url"])
            return f"opened pull request #{pr['number']}: {pr['html_url']}"
        if name == "push_changes":
            subprocess.run(["git", "add", "-A"], cwd=repo)
            subprocess.run(["git", "commit", "-m", a["message"]], cwd=repo, capture_output=True)
            subprocess.run(["git", "push"], cwd=repo, env=env, capture_output=True)
            return "pushed"
        if name == "subscribe":
            brain_call("subscribe", wid=wid, thread=a["thread"])
            return "subscribed"
        if name == "update_ticket":
            if ticket_id:
                try:
                    bugui_update(ticket_id, status=a["status"], comment=a.get("note", ""))
                except Exception as e:
                    return f"update_ticket failed: {type(e).__name__}"
            return "ok"
        if name == "report":
            brain_call("report", wid=wid, status=a["status"], summary=a["summary"])
            return "ok"
        return "ok"

    policy_line = "\nPolicy for this ticket: silent. Do not report or change anything without evidence." if policy == "silent" else ""
    history = [{"role": "system", "content": WORKER_SYSTEM + policy_line},
               {"role": "user", "content": f"TICKET kind={kind}\nTASK: {task}\n\n{context}"}]
    for _ in range(40):
        inbox = brain_call("inbox", wid=wid)
        if inbox:
            history.append({"role": "user", "content": "NEW EVENTS\n" + "\n\n".join(inbox)})
        try:
            msg, cost, use = llm(WORKER_MODEL, history, WORKER_TOOLS, 1500)
        except Exception:
            time.sleep(5)
            continue
        brain_call("log", wid=wid, cost=cost, tokens=use.get("total_tokens", 0), text="")
        history.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
        calls = msg.get("tool_calls") or []
        if not calls:
            history.append({"role": "user", "content": "Call a tool, or wait, or report."})
            continue
        done = False
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            res = worker_tool(fn, args)
            history.append({"role": "tool", "tool_call_id": c["id"], "content": str(res)})
            done = done or fn == "report"
        if done:
            break


# ------------------------------------------------------------------------------------------- API
app = FastAPI(title="Superagent bug logging app — brain")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.on_event("startup")
def start():
    for f in (router_loop, telegram_loop, github_loop):
        threading.Thread(target=f, daemon=True).start()
    st.log("system", "The brain is running.")


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
    return {"log": d["log"][after + 1:], "counts": d["counts"],
            "workers": [{k: v for k, v in w.items() if k != "activity"} for w in d["workers"].values()]}


@app.get("/health")
def health():
    return {"ok": True, "started": st.d["started"]}
