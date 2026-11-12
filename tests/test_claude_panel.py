"""Claude panel: one request per press, a proposal first, nothing changes without Apply, the format rules hold."""
import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import assistant, handoff, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "")          # no real call can happen in a test
    calls = []

    def ask(jpg, settings, request):
        calls.append(request)
        by = {s["name"]: s for s in settings}
        ap = by["aperture"]
        other = next(c for c in ap["choices"] if c["value"] != ap["value"])
        return {"changes": [{"name": "aperture", "value": other["value"], "label": other["label"], "from_label": ap["label"]},
                            {"name": "picture_profile", "value": 1, "label": "PP1", "from_label": "Off"}],
                "explanation": "Moody on purpose.", "tips": "", "dropped": []}
    monkeypatch.setattr(assistant, "ask", ask)
    cam = ZVE10(ev=10, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, calls


def test_direct_is_a_proposal_and_changes_nothing(app):
    from camctl.drivers.sony import PROPS
    c, cam, calls = app
    before = cam.p[PROPS["aperture"]].value
    r = c.post("/api/claude/direct", json={"request": "Moody interview", "mode": "photo"}).json()
    assert len(calls) == 1 and "Moody interview" in calls[0]
    assert [x["name"] for x in r["changes"]] == ["aperture"]           # format rules: picture profile never offered
    assert cam.p[PROPS["aperture"]].value == before                    # nothing applied without his press


def test_apply_sets_verifies_and_keeps_his_choice(app):
    from camctl.drivers.sony import PROPS
    c, cam, _ = app
    prop = c.post("/api/claude/direct", json={"request": "x"}).json()["changes"][0]
    r = c.post("/api/claude/apply", json={"changes": [prop, {"name": "still_format", "value": 3}], "say": "Claude set x."}).json()
    assert r["verified"] == ["aperture"] and cam.p[PROPS["aperture"]].value == prop["value"]
    assert cam.p[0xD253].value != 3 if cam.p.get(0xD253) else True    # RAW + JPEG is never touched
    st = c.get("/api/claude/state").json()
    assert st["intent"]["values"] == {"aperture": prop["value"]} and st["intent"]["say"] == "Claude set x."


def test_best_take_needs_two_takes_and_calls_nobody(app, monkeypatch):
    from camctl import bestpick
    c, _, _ = app
    called = []
    monkeypatch.setattr(bestpick, "ask_claude", lambda cands: called.append(1) or {"best": [], "why": ""})
    r = c.post("/api/claude/best").json()
    assert r["best"] == [] and "fewer than two takes" in r["say"] and not called


def test_review_without_a_take_says_so(app):
    c, _, _ = app
    r = c.post("/api/claude/review").json()
    assert "no take" in r["say"].lower() and r["changes"] == []


def test_local_rating_prefers_sharp():
    from camctl import bestpick
    rng = np.random.default_rng(3)
    im = (rng.random((360, 480, 3)) * 255).astype(np.uint8)
    sharp = cv2.imencode(".jpg", im)[1].tobytes()
    soft = cv2.imencode(".jpg", cv2.GaussianBlur(im, (0, 0), 4))[1].tobytes()
    ranked = bestpick.rank([(1, soft), (2, sharp)])
    assert ranked[0]["id"] == 2 and ranked[0]["score"] > ranked[1]["score"]
