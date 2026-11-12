"""Changing and removing the Claude key in All settings › Studio › Claude key. Fake keys only; no request leaves
the machine. The old key stays in use until a new one has passed the one test on his Save press."""
import logging
import os

import pytest

from test_claude_key import FAKE, FAKE2, client, store  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")


def test_change_to_a_good_key(client, store):
    local, lan, checked = client
    assert local.post("/api/claude/key", json={"key": FAKE}).json()["message"].startswith("Key saved.")
    r = local.post("/api/claude/key", json={"key": FAKE2}).json()
    assert r["ok"] and r["message"].startswith("New key saved and working.")
    assert store.load() == FAKE2 and len(checked) == 2                     # exactly one test per Save press
    s = local.get("/api/claude/state").json()
    assert s["source"] == "app" and s["since"] and s["last_ok"] is None or s["last_ok"] is not None


def test_a_bad_new_key_keeps_the_old_one(client, store):
    local, lan, checked = client
    local.post("/api/claude/key", json={"key": FAKE})
    r = local.post("/api/claude/key", json={"key": FAKE2 + "w"})
    assert r.status_code == 400 and "old key is still in use" in r.json()["error"]
    assert store.load() == FAKE                                              # the old key was not touched


def test_remove(client, store):
    local, lan, checked = client
    local.post("/api/claude/key", json={"key": FAKE})
    r = local.post("/api/claude/key", json={"action": "remove"}).json()
    assert r["set"] is False and "Claude is off" in r["message"] and not store.PATH.exists()


def test_remove_with_the_windows_key_present(client, store, monkeypatch):
    local, lan, checked = client
    local.post("/api/claude/key", json={"key": FAKE})
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE2)
    s = local.get("/api/claude/state").json()
    assert s["source"] == "app" and s["env_too"] is True                    # the app key wins while it is there
    r = local.post("/api/claude/key", json={"action": "remove"}).json()
    assert r["source"] == "env" and "key set in Windows" in r["message"]
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE2                         # the Windows setting is left alone


def test_another_device_cannot_change_or_remove(client, store):
    local, lan, checked = client
    local.post("/api/claude/key", json={"key": FAKE})
    assert lan.post("/api/claude/key", json={"key": FAKE2}).status_code == 403
    assert lan.post("/api/claude/key", json={"action": "remove"}).status_code == 403
    assert store.load() == FAKE and lan.get("/api/claude/state").json()["local"] is False


def test_nothing_key_like_in_answers_or_logs(client, store, caplog):
    local, lan, checked = client
    caplog.set_level(logging.DEBUG)
    bodies = [local.post("/api/claude/key", json={"key": FAKE}).text, local.post("/api/claude/key", json={"key": FAKE2 + "w"}).text,
              local.get("/api/claude/state").text, local.get("/api/claude/key").text, local.get("/api/ready").text]
    for b in bodies:
        assert "sk-ant" not in b and FAKE[-12:] not in b and FAKE2[-12:] not in b
    assert "sk-ant" not in caplog.text
