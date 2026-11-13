"""Claude works differently by mode, format and tool: one situation block for every camera button (built in one
place, added in one place), cards and chips per mode / tool in the panel, and never advice on looks or grading."""
import json
import re
from pathlib import Path

import pytest

from camctl import assistant

ROOT = Path(__file__).resolve().parents[1]


def test_role_and_format_rule_per_mode():
    p = assistant.situation_text({"mode": "photo", "format": "RAW + JPEG · 16:9", "pp_kind": "standard", "aspect": "16:9"})
    v = assistant.situation_text({"mode": "video", "format": "S-Log3 · 4K · 25p · 100 Mbps", "pp_kind": "log",
                                  "shutter_rule": "25p with 50 Hz mains: 1/50 s, never changed",
                                  "last_take": "aperture F4.0, iso 800"})
    assert "photographer" in p and "safe handheld" in p and "finished picture" in p and "Aspect ratio: 16:9" in p
    assert "director of photography" in v and "continuity" in v and "grading later" in v and "1/50 s" in v
    assert "Last take: aperture F4.0" in v
    for t in (p, v):
        assert assistant.NO_LOOKS in t


@pytest.mark.parametrize("key,words", [("focusstack", "sharpest aperture"), ("long", "never advise a faster shutter"),
                                       ("double", "dark area in the first"), ("pull", "distance between point A"),
                                       ("reel", "3 seconds"), ("sound", "microphone"), ("teleprompter", "eye line"),
                                       ("shotlist", "next shot")])
def test_each_tool_changes_the_priorities(key, words):
    t = assistant.situation_text({"mode": "photo", "tools": [{"key": key, "detail": key}]})
    assert words in t and assistant.TOOL_RULES[key] in t


def test_no_prompt_advises_looks():
    texts = [v for k, v in vars(assistant).items() if k.endswith("SYSTEM") and isinstance(v, str)]
    texts += list(assistant.TOOL_RULES.values()) + list(assistant.ROLE.values())
    from camctl import shotplan, bestpick, webui
    texts += [v for m in (shotplan, bestpick) for k, v in vars(m).items() if k.endswith("SYSTEM") and isinstance(v, str)]
    src = (ROOT / "camctl" / "webui.py").read_text(encoding="utf-8")
    assert "RAW photos / S-Log3 video" not in src                                     # no fixed format in prompts
    for t in texts:
        low = t.lower()
        for bad in ("apply a lut", "add a lut", "grade it", "teal", "orange look", "film look", "use a preset"):
            assert bad not in low, (bad, t[:80])


class _Stub:
    def __init__(self):
        self.bodies = []


@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    stub = _Stub()
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "sk-fake")

    def send(body, key):                                  # what really goes out, after shape()
        stub.bodies.append(body)
        text = json.dumps({"settings": [], "explanation": "Fine.", "tips_for_user": ""})
        return {"model": body["model"], "stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {}}
    monkeypatch.setattr(assistant, "_send", send)
    cam = ZVE10(ev=9, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        st.props(max_age=0)
        yield c, cam, st, stub


def _system(stub):
    return stub.bodies[-1]["system"]


def test_every_camera_button_carries_the_situation_and_tools_leave_no_trace(studio):
    c, cam, st, stub = studio
    c.post("/api/claude/direct", json={"request": "make it moody", "mode": "photo"})
    s = _system(stub)
    assert "Situation: photo mode." in s and "photographer" in s and "Tool in use" not in s
    c.post("/api/play", json={"double": "on"})
    c.post("/api/claude/context", json={"look": "Warm print"})
    c.post("/api/claude/direct", json={"request": "x", "mode": "photo"})
    s = _system(stub)
    assert "Tool in use: double exposure" in s and "previewing the look 'Warm print'" in s
    c.post("/api/play", json={"double": "off"})
    c.post("/api/claude/context", json={"look": None})
    c.post("/api/claude/direct", json={"request": "x", "mode": "photo"})
    s = _system(stub)
    assert "Tool in use" not in s and "previewing the look" not in s                  # switched off: no trace
    c.post("/api/claude/context", json={"panel": "focusstack"})
    c.post("/api/claude/direct", json={"request": "x", "mode": "photo"})
    assert "Focus stack" in _system(stub)
    c.post("/api/claude/context", json={"panel": None})
    # video: the role and the shutter rule change
    c.post("/api/set", json={"name": "exposure_mode", "value": 491603})
    st.props(max_age=0)
    c.post("/api/claude/direct", json={"request": "x", "mode": "video"})
    s = _system(stub)
    assert "Situation: video mode." in s and "director of photography" in s and "Shutter rule: 25p" in s
    assert "A log profile is on" in s


def test_non_camera_calls_get_no_situation(studio):
    c, cam, st, stub = studio
    c.post("/api/video/script", json={"topic": "hello", "seconds": 20})
    assert "Situation:" not in str(stub.bodies[-1].get("system", ""))


def test_panel_cards_and_chips_follow_mode_and_tool():
    page = (ROOT / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert 'k:"review",t:"Review my last take",l:"What a director would say",modes:["video"]' in page
    assert 't:"Review my last photo"' in page and 't:"Pick the best photo"' in page
    assert "${clCards().map(" in page and "${clChips().map(" in page
    i = page.index("const CL_CHIPS=")
    chips = page[i:page.index("};", i)]
    for k in ("focusstack", "long", "double", "pull", "reel", "sound", "teleprompter", "shotlist", "photo", "video"):
        assert re.search(rf"\b{k}:\[", chips), k
    assert "return S.ctxPanel" in page or 'if(["sound","teleprompter","shotlist"].includes(S.ctxPanel)) return S.ctxPanel;' in page
