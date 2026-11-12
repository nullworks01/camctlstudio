"""Getting ready: shown only when the camera or the Claude key is missing. At start nothing calls Claude; a key is
tested only on his Save press. A failed Claude call (key, credit, internet) is said in one plain sentence, where he
pressed and again on the next start. Only fake keys; no request ever leaves the machine."""
import os
import urllib.error

import pytest

from test_claude_key import FAKE, client, store  # noqa: F401  (fixtures)


def _calls_guard(monkeypatch):
    """Any real request to Claude fails the test."""
    from camctl import assistant
    monkeypatch.setattr(assistant, "_post", lambda *a, **k: pytest.fail("a Claude call was made"))


def test_no_key_shows_the_claude_line(client, monkeypatch):
    local, lan, checked = client
    _calls_guard(monkeypatch)
    r = local.get("/api/ready").json()
    assert r["show"] and r["claude"]["saved"] is False and r["claude"]["local"] is True
    assert r["claude"]["problem"] is None and checked == []          # nothing was tested at start


def test_key_saved_and_camera_there_screen_not_shown(store, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import video, webui
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(video, "find_ffmpeg", lambda: "ffmpeg.exe")         # every library present
    local = TestClient(webui.build_app(_studio_with(ZVE10(ev=10))), client=("127.0.0.1", 50001))
    local.__enter__()
    checked = []
    _calls_guard(monkeypatch)
    store.save(FAKE) if os.name == "nt" else monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    r = local.get("/api/ready").json()
    assert r["camera"]["ok"] and r["claude"]["saved"] and r["show"] is False
    assert checked == []


def test_saving_a_bad_key_is_said_and_not_kept(client):
    local, lan, checked = client
    r = local.post("/api/claude/key", json={"key": FAKE[:-3] + "wrong"})
    assert r.status_code == 400 and "didn't accept" in r.json()["error"]
    assert local.get("/api/ready").json()["claude"]["saved"] is False


@pytest.mark.parametrize("err,kind,words", [
    (urllib.error.HTTPError("u", 401, "x", {}, None), "wrong", "didn't accept the key"),
    (urllib.error.URLError("no route"), "offline", "internet"),
])
def test_a_failed_call_is_said_plainly_and_again_at_the_next_start(client, store, monkeypatch, err, kind, words):
    local, lan, checked = client
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    from camctl import assistant
    monkeypatch.setattr(assistant.time, "sleep", lambda s: None)

    def boom(*a, **k):
        if isinstance(err, urllib.error.HTTPError):
            err.read = lambda: b"{}"
        raise err
    monkeypatch.setattr(assistant, "_post", boom)
    with pytest.raises(assistant.AskError) as e:
        assistant.ask(b"\xff\xd8", [], "test")
    text = str(e.value)
    assert words in text and "API" not in text and "401" not in text      # plain words only
    r = local.get("/api/ready").json()                                  # the next start shows it
    assert r["show"] and r["claude"]["problem"]["kind"] == kind and words in r["claude"]["problem"]["text"]
    assistant.usage.record({})                                          # a call that works clears it
    assert local.get("/api/ready").json()["claude"]["problem"] is None


def test_no_credit_is_said(client, store, monkeypatch):
    from camctl import assistant
    assert "no credit" in str(assistant.AskError("Claude API error 400: Your credit balance is too low"))
    assert assistant.problem()["kind"] == "nocredit"


def test_camera_missing_is_said(client, monkeypatch):
    local, lan, checked = client
    from camctl import webui
    monkeypatch.setattr(webui.Studio, "health", lambda self: {"state": "offline", "message": "The camera is off or asleep."})
    r = local.get("/api/ready").json()
    assert r["show"] and r["camera"]["ok"] is False and r["camera"]["text"] == "The camera is off or asleep."


def test_on_the_ipad_the_key_is_entered_on_the_laptop(client):
    local, lan, checked = client
    r = lan.get("/api/ready").json()
    assert r["claude"]["local"] is False
    assert lan.post("/api/claude/key", json={"key": FAKE}).status_code == 403 and checked == []


def test_the_page_has_the_screen_and_never_calls_claude_at_start():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert 'id="ready"' in page and "Continue without Claude" in page
    start = page[page.index("GETTING READY"):]
    assert "/api/claude/direct" not in start and "/api/claude/key" not in start   # only /api/ready at start


def test_started_with_the_camera_off_is_said_not_ok():
    """Studio started while the camera is off (charging): no 'ok', a plain sentence after a few seconds."""
    import time as _t
    from camctl import webui
    st = webui.Studio("10.0.0.38")
    st.cam, st.last_error, st.choice = None, "Camera not reachable on Wi-Fi (10.0.0.38)", "auto"
    st._present = lambda: False
    h = st.health()
    assert h["state"] == "recovering" and "Reconnecting" in h["message"]
    st.fail_since = _t.time() - 10
    h = st.health()
    assert h["state"] == "absent" and "Check that it is on" in h["message"] and "error" not in h["message"].lower()


def test_opening_settings_or_claude_closes_the_ready_card():
    """Audit 12 Nov 2026: All settings opened (key S) over the Getting ready card left both on screen."""
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    for fn in ("function asOpen(", "function clOpen("):
        body = page[page.index(fn):page.index(fn) + 400]
        assert "rdClose()" in body, fn
