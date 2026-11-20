"""Offline check of follow-up routing: same ticket while a PR is open, new ticket after it merges.
Models, the Bug UI and Worker sessions are stubbed; nothing touches the network or real state.
Run from agent/:  venv/Scripts/python.exe test_routing.py"""
import os, sys, tempfile
tmp = tempfile.mkdtemp()
os.environ.update(STATE_PATH=os.path.join(tmp, "state.json"), WORK_ROOT=os.path.join(tmp, "work"), BUG_UI_URL="",
                  TELEGRAM_TOKEN="", AGENT_GH_TOKEN="", GH_TEAM="mosrihari")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain as b
calls = []
b.bugui_update = lambda tid, **f: calls.append(("update", tid, f))
b.bugui_create = lambda kind, **f: (calls.append(("create", kind, f)), {"id": "ENH-2"})[1]
b.classify = lambda s, a, t: {"kind": "enhancement", "reason": "stub"}
woken = []; b.wake_worker = lambda w: woken.append(w)
d = b.st.d
d["workers"]["W3"] = {"id": "W3", "task": "Hello Srihari page", "kind": "enhancement", "ticket_id": "ENH-1",
    "threads": ["tg:1", "gh:2"], "policy": "normal", "status": "running", "started": b.now(), "reporter": "Srihari",
    "pr": "https://github.com/x/y/pull/2"}
d["threads"]["tg:1"] = {"id": "tg:1", "subscribers": ["W3"], "triggers": []}
d["threads"]["gh:2"] = {"id": "gh:2", "subscribers": ["W3"], "triggers": []}
d["inbox"]["W3"] = []

# 1. Telegram follow-up while PR open -> same Worker, recorded on ENH-1
b.intake("telegram", "tg:1", "Srihari", "make the card blue", ext="a")
assert woken == ["W3"] and d["queue"] == [], (woken, d["queue"])
assert ("update", "ENH-1", {"comment": "Follow-up from Srihari via telegram: make the card blue"}) in calls, calls
# 1b. GitHub PR comment while open -> also recorded
b.intake("github", "gh:2", "mosrihari", "add a test for it", ext="b")
assert any(c[1] == "ENH-1" and "via github" in c[2].get("comment", "") for c in calls)
print("1 ok: follow-ups while PR open stay on ENH-1 (telegram + github), recorded on board")

# 2. Merge -> W3 merged, leaves tg chat, keeps gh thread, merge notice still reaches W3
woken.clear(); calls.clear()
b.intake("github", "gh:2", "a teammate", "pull request #2 merged", ext="m2")
w = d["workers"]["W3"]
assert w["merged"] and w["threads"] == ["gh:2"] and d["threads"]["tg:1"]["subscribers"] == [] and woken == ["W3"]
assert not calls, calls   # merge notice is not a person's follow-up
print("2 ok: merge marks W3 merged, detaches tg chat, merge notice still delivered")

# 3. New request in chat after merge -> new ticket ENH-2 queued for the Router, not W3
woken.clear()
tid = b.intake("telegram", "tg:1", "Srihari", "Can you add one more endpoint saying hello apekshitha", ext="c")
assert woken == [] and d["queue"] == [tid] and d["triggers"][tid]["ticket_id"] == "ENH-2", (woken, d["queue"])
assert calls[-1][0] == "create"
print("3 ok: post-merge request becomes ENH-2 and goes to the Router (correlate skipped merged W3)")

# 4. Race: request reached W3 in the same batch as the merge -> W3 redirects it
d["queue"].clear(); calls.clear()
d["threads"]["tg:1"]["subscribers"].append("W3"); w["threads"].append("tg:1")
t2 = b.intake("telegram", "tg:1", "Srihari", "also add a goodbye page", ext="e")
assert b.brain_call("redirect", wid="W3", trigger=t2, reason="new after merge") == "ok"
assert b.brain_call("redirect", wid="W3", trigger=t2, reason="x") == "already redirected"
assert "W3" not in d["threads"]["tg:1"]["subscribers"]
b.route_new(t2, kind="enhancement", exclude="W3")
assert d["queue"] == [t2] and "REDIRECTED by W3" in b.inject_view(d["triggers"][t2])
print("4 ok: redirect hands the request to a new ticket, Router sees REDIRECTED")
print("router view:", b.router_view().splitlines()[1])

# 5. Board replies
b.run_worker = lambda *a, **k: None
b._mark_busy = lambda w: None
calls.clear(); d["queue"].clear()
w4 = b.do_start_worker("Goodbye page", "enhancement", "ENH-3", "tg:1", "normal").split()[-1]
assert "bugui:ENH-3" in d["workers"][w4]["threads"], d["workers"][w4]["threads"]
b.brain_call("pr", wid=w4, number=7, url="https://github.com/x/y/pull/7")
woken.clear(); calls.clear()
b.intake("bugui", "bugui:ENH-3", "Srihari", "use a bigger font")
assert woken == [w4] and d["queue"] == [] and not any(c[0] == "update" for c in calls), (woken, calls)
print("5a ok: card reply while PR open goes to the same Worker, not double-recorded")
b.intake("github", "gh:7", "a teammate", "pull request #7 merged", ext="m7")
assert "bugui:ENH-3" not in d["workers"][w4]["threads"] and "tg:1" not in d["workers"][w4]["threads"]
woken.clear(); calls.clear()
t6 = b.intake("bugui", "bugui:ENH-3", "Srihari", "now add a contact page")
assert woken == [] and d["queue"] == [t6] and calls[-1][0] == "create", (woken, d["queue"], calls)
print("5b ok: card reply after merge becomes a new ticket")
calls.clear(); b.send_reply("bugui:ENH-3", "On it: ENH-2.")
assert calls == [("update", "ENH-3", {"comment": "Agent: On it: ENH-2."})], calls
print("5c ok: Router reply on a board thread appears on the card")
d["queue"].clear()
b.intake("github", "gh:99", "a teammate", "pull request #99 merged", ext="m99")
assert d["queue"] == [] and d["triggers"][max(d["triggers"], key=lambda x: int(x[1:]))]["disposition"] == "dropped"
print("6 ok: merge of a PR no Worker owns is dropped, no model call")
print("all routing checks passed")
