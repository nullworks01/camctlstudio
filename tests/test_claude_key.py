"""The Claude key inside Studio: saved encrypted, never shown again, laptop only, the environment as fallback.
Only fake keys are used here; no request ever leaves the machine."""
import logging
import os

import pytest

FAKE = ("sk-" + "ant-test-") + "Q7x" * 12            # a made-up key, shaped like a real one
FAKE2 = ("sk-" + "ant-test-") + "Zr9" * 12


@pytest.fixture
def store(monkeypatch, tmp_path):
    from camctl import keystore
    monkeypatch.setattr(keystore, "BASE", tmp_path)
    monkeypatch.setattr(keystore, "PATH", tmp_path / "claude_key.bin")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from camctl import assistant
    monkeypatch.setattr(assistant, "_env_key", lambda: os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return keystore


@pytest.fixture
def client(store, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import assistant, handoff, webui
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    checked = []

    def fake_test(key):
        checked.append(1)
        if "wrong" in key or key == FAKE2 + "w":
            return "wrong", "Claude didn't accept this key. Check that you copied all of it, or make a new one."
        return "ok", "Claude is connected."
    monkeypatch.setattr(assistant, "test_key", fake_test)
    app = webui.build_app(_studio_with(ZVE10(ev=10)))
    local = TestClient(app, client=("127.0.0.1", 50001))
    lan = TestClient(app, client=("10.0.0.50", 50002))
    with local, lan:
        yield local, lan, checked


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_saved_encrypted_and_read_back(store):
    store.save(FAKE)
    raw = store.PATH.read_bytes()
    assert FAKE.encode() not in raw and b"sk-ant" not in raw          # never clear text on disk
    assert store.load() == FAKE
    assert store.remove() and store.load() == ""


def test_shape_check(store):
    with pytest.raises(ValueError):
        store.save("hello")
    assert not store.looks_like_a_key("sk-ant-")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_save_replace_remove_and_nothing_comes_back(client, store, caplog):
    local, lan, checked = client
    caplog.set_level(logging.DEBUG)
    k = local.get("/api/claude/key").json()
    assert k["set"] is False and k["source"] == "" and k["local"] is True and k["since"] is None
    r = local.post("/api/claude/key", json={"key": FAKE})
    assert r.status_code == 200 and r.json()["message"] == "Key saved. Claude is connected." and len(checked) == 1
    st = local.get("/api/claude/state").json()
    assert st["has_key"] is True and st["source"] == "app"
    r2 = local.post("/api/claude/key", json={"key": FAKE2})                 # replace
    assert r2.status_code == 200 and store.load() == FAKE2
    texts = [r.text, r2.text, local.get("/api/claude/key").text, local.get("/api/claude/state").text,
             local.get("/api/settings").text]
    for txt in texts + [caplog.text]:
        assert "sk-ant" not in txt and FAKE[12:24] not in txt and FAKE2[12:24] not in txt
    r3 = local.post("/api/claude/key", json={"action": "remove"})
    assert r3.json()["set"] is False and store.load() == ""


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_wrong_key_is_not_saved(client, store):
    local, _, _ = client
    r = local.post("/api/claude/key", json={"key": ("sk-" + "ant-test-wrong-") + "a" * 20})
    assert r.status_code == 400 and "didn't accept" in r.json()["error"] and "sk-ant" not in r.text
    assert store.load() == ""


def test_refused_over_the_lan(client, store):
    _, lan, checked = client
    r = lan.post("/api/claude/key", json={"key": FAKE})
    assert r.status_code == 403 and r.json()["error"] == "Add the key on the laptop."
    assert not checked and not store.PATH.exists()
    assert lan.get("/api/claude/state").json()["local"] is False


def test_environment_is_the_fallback_and_the_saved_key_wins(store, monkeypatch):
    from camctl import assistant
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE2)
    assert assistant._key() == FAKE2 and assistant.key_source() == "env"
    if os.name == "nt":
        store.save(FAKE)
        assert assistant._key() == FAKE and assistant.key_source() == "app"
        store.remove()
        assert assistant._key() == FAKE2
