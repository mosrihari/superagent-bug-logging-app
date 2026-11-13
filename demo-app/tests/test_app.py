"""2 tests pass today; no test covers the idle-session bug yet — that's the agent's first ticket."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app  # noqa: E402


def client():
    return app.test_client()


def test_login_returns_a_token():
    r = client().post("/login", json={"user": "priya"})
    assert r.status_code == 200
    assert r.json["token"].startswith("tok-priya-")


def test_whoami_with_no_token_is_unauthorized():
    r = client().get("/whoami")
    assert r.status_code == 401
