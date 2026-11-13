"""A tiny Flask app for the Super Agent to work on. Push this to its own GitHub repository (AGENT_REPO)
and turn on GitHub Actions so it has CI. See README.md.

It has one planted bug: a session that has been idle past SESSION_TTL_S is treated as simply "not found"
(a plain 401 with no detail), which is exactly the vague symptom a user would report as "login fails" —
a good first ticket for the agent to reproduce and fix with a real failing test.

Logging ships to Axiom when AXIOM_TOKEN/AXIOM_DATASET are set (see log_error below); otherwise it just
prints, so this app runs with nothing configured.
"""
import json
import os
import time
import urllib.request

from flask import Flask, jsonify, request

app = Flask(__name__)
SESSION_TTL_S = 15 * 60
sessions = {}  # token -> {"user": str, "created": float}

AXIOM_TOKEN = os.environ.get("AXIOM_TOKEN", "")
AXIOM_DATASET = os.environ.get("AXIOM_DATASET", "")


def log_error(kind, **fields):
    """Structured error log. Ships to Axiom's ingest API if configured; always prints locally too.
    Keep this structured (a signature + fields), not a free-text stack trace dump — see the project's
    docs/architecture.md on why raw logs should never be what an agent reads first."""
    event = {"_time": time.time(), "kind": kind, **fields}
    print(json.dumps(event), flush=True)
    if AXIOM_TOKEN and AXIOM_DATASET:
        try:
            req = urllib.request.Request(
                f"https://api.axiom.co/v1/datasets/{AXIOM_DATASET}/ingest", data=json.dumps([event]).encode(),
                headers={"Authorization": f"Bearer {AXIOM_TOKEN}", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # never let logging itself break a request


@app.post("/login")
def login():
    user = (request.json or {}).get("user", "")
    token = f"tok-{user}-{int(time.time())}"
    sessions[token] = {"user": user, "created": time.time()}
    return jsonify({"token": token})


@app.get("/whoami")
def whoami():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    session = sessions.get(token)
    if session is None:
        # The planted bug: an idle-but-real session and a session that never existed look identical here.
        log_error("auth_401", token_known=False, reason="not_found")
        return jsonify({"error": "not found"}), 401
    if time.time() - session["created"] > SESSION_TTL_S:
        log_error("auth_401", token_known=False, reason="not_found")  # should be "expired", but isn't yet
        return jsonify({"error": "not found"}), 401
    return jsonify({"user": session["user"]})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
