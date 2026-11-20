"""Talk to the brain from your terminal, without needing real Telegram/Axiom/GitHub set up yet.

    python agent/drive.py bug "Priya" "login fails after an idle session, shows 401"
    python agent/drive.py enhancement "Arjun" "add a health check endpoint that returns ok"
    python agent/drive.py alert "payment-timeout" "502 from the payments service, 14 times in 5 minutes"
    python agent/drive.py show
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_dotenv(path):
    """So this works whether or not you remembered to export .env in this terminal."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = re.sub(r"(?<!\\)#.*$", "", line).strip()
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8000")
BUG_UI_URL = os.environ.get("BUG_UI_URL", "http://localhost:3000")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
BUG_UI_TOKEN = os.environ.get("BUG_UI_TOKEN", "")
AXIOM_WEBHOOK_TOKEN = os.environ.get("AXIOM_WEBHOOK_TOKEN", "")


def post_json(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode(errors="replace")}


def post(path, body, headers=None):
    return post_json(BRAIN_URL + path, body, headers)


def get(path):
    req = urllib.request.Request(BRAIN_URL + path, headers={"x-admin": ADMIN_SECRET})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("bug", "enhancement") and len(sys.argv) >= 4:
        # Files a REAL ticket through the Bug UI's own API — same path a person using the web form
        # takes. The Bug UI then notifies the brain itself (see bug-ui/app/api/tickets/route.ts), so
        # the ticket actually appears on the board, instead of only existing in the brain's own state.
        reporter, description = sys.argv[2], sys.argv[3]
        print(post_json(BUG_UI_URL + "/api/tickets", {"type": cmd, "title": description[:80],
                                                       "description": description, "source": "ui",
                                                       "reporter": reporter}))
    elif cmd == "alert" and len(sys.argv) >= 4:
        print(post("/webhooks/axiom", {"signature": f"{sys.argv[2]}-{int(time.time())}", "description": sys.argv[3]},
                   headers={"x-axiom-token": AXIOM_WEBHOOK_TOKEN}))
    elif cmd == "show":
        print(json.dumps(get("/events"), indent=2)[-4000:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
