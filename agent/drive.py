"""Talk to the brain from your terminal, without needing real Telegram/Axiom/GitHub set up yet.

    python agent/drive.py bug "Priya" "login fails after an idle session, shows 401"
    python agent/drive.py enhancement "Arjun" "add a health check endpoint that returns ok"
    python agent/drive.py alert "payment-timeout" "502 from the payments service, 14 times in 5 minutes"
    python agent/drive.py show
"""
import json
import os
import sys
import urllib.request

BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8000")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")


def post(path, body, headers=None):
    req = urllib.request.Request(BRAIN_URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get(path):
    req = urllib.request.Request(BRAIN_URL + path, headers={"x-admin": ADMIN_SECRET})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("bug", "enhancement") and len(sys.argv) >= 4:
        # Simulates a ticket already filed in the Bug UI, the way its webhook would report it.
        print(post("/webhooks/bugui", {"id": f"local-{cmd}-1", "type": cmd, "description": sys.argv[3],
                                       "reporter": sys.argv[2]}))
    elif cmd == "alert" and len(sys.argv) >= 4:
        print(post("/webhooks/axiom", {"signature": sys.argv[2], "description": sys.argv[3]}))
    elif cmd == "show":
        print(json.dumps(get("/events"), indent=2)[-4000:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
