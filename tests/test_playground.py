"""Playground experiments on the simulator: double exposure, long exposure (+ practice), shoot for a mood (stubbed
Claude), focus pull. Every photo is a press here; results are new files; originals never change."""
import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from camctl import playground


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
    cam = ZVE10(ev=9, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, st, handoff


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------- double exposure
def test_blends_are_average_and_screen(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    Image.new("RGB", (60, 40), (100, 100, 100)).save(a, quality=100)
    Image.new("RGB", (30, 20), (200, 50, 0)).save(b, quality=100)            # another size: fitted, not cropped
    soft = playground.blend(a, b, "soft").getpixel((10, 10))
    bright = playground.blend(a, b, "bright").getpixel((10, 10))
    assert abs(soft[0] - 150) <= 3 and abs(soft[2] - 50) <= 3
    assert abs(bright[0] - round(255 - (155 * 55) / 255)) <= 3 and bright[0] >= max(100, 200) - 2


def test_double_exposure_two_presses_one_new_photo(studio):
    c, cam, st, handoff = studio
    assert c.post("/api/play", json={"double": "on"}).json()["double"]["on"] is True
    r1 = c.post("/api/shoot", json={}).json()
    assert r1["double"]["step"] == 2 and c.get("/api/play/image/first.jpg").status_code == 200
    r2 = c.post("/api/shoot", json={}).json()
    shoot = Path(handoff.current()["path"])
    out = shoot / "PLAYGROUND" / "Double exposure" / r2["double"]["file"]
    assert out.exists() and r2["double"]["file"].endswith("Bright.jpg")              # film-style by default
    originals = [p for p in r1["shoot"] + r2["shoot"] if p.lower().endswith(".jpg")]
    before = {p: _sha(p) for p in originals}
    s = c.post("/api/play", json={"double": "soft"}).json()
    assert s["double"]["result"].endswith("Soft.jpg") and not out.exists()           # his choice replaces Studio's blend
    assert {p: _sha(p) for p in originals} == before                                  # both photos untouched
    s = c.post("/api/play", json={"double": "reset"}).json()
    assert s["double"]["step"] == 1 and s["double"]["on"] is True


# ---------------------------------------------------------------- long exposure
def test_long_exposure_setup_only_on_apply_and_back(studio):
    from camctl.drivers.sony import PROPS
    c, cam, st, handoff = studio
    c.post("/api/post", json={"post": True, "mode": "photo", "setup": True})
    before = {k: cam.p[PROPS[k]].value for k in ("shutter_speed", "iso", "aperture")}
    s = c.post("/api/play", json={"long": True}).json()
    assert s["long"]["offered"] == [10, 20, 30] and not s["long"]["applied"]
    assert {k: cam.p[PROPS[k]].value for k in ("shutter_speed", "iso", "aperture")} == before    # nothing yet
    r = c.post("/api/play/long/apply", json={"seconds": 20}).json()
    assert r["say"].startswith("Locked for a long exposure: 20 s")
    from camctl.presets import shutter_seconds
    assert shutter_seconds(cam.p[PROPS["shutter_speed"]].value) == 20 and cam.p[PROPS["focus_mode"]].value == 1
    assert cam.p[PROPS["iso"]].value == 100 and cam.p[PROPS["aperture"]].value == 800
    c.post("/api/play/long/restore", json={})
    assert {k: cam.p[PROPS[k]].value for k in ("shutter_speed", "iso", "aperture")} == before


def test_countdown_then_finishing_without_a_second_press(studio, monkeypatch):
    c, cam, st, handoff = studio
    c.post("/api/play", json={"long": True})
    c.post("/api/play/long/apply", json={"seconds": 10})
    real = cam.capture
    calls = []

    def slow_capture(timeout=20.0, af=True):
        calls.append(timeout)
        time.sleep(1.5)
        return real(timeout, af)
    monkeypatch.setattr(cam, "capture", slow_capture)
    t = threading.Thread(target=lambda: c.post("/api/shoot", json={}))
    t.start()
    time.sleep(0.6)
    s = c.get("/api/play").json()["long"]
    assert s["phase"] == "open" and 8 <= s["left"] <= 10
    t.join(30)
    assert c.get("/api/play").json()["long"]["phase"] is None
    assert len(calls) == 1 and calls[0] >= 2 * 10 + 40                                # one press, a long enough wait


def test_no_long_times_means_practice_only(studio):
    from camctl.drivers.sony import PROPS
    c, cam, st, handoff = studio
    sh = cam.p[PROPS["shutter_speed"]]
    from camctl.presets import shutter_seconds
    sh.choices = [v for v in sh.choices if not (shutter_seconds(v) or 0) >= 10]
    st.props(max_age=0)
    assert c.get("/api/play").json()["long"]["offered"] == []
    r = c.post("/api/play/long/apply", json={"seconds": 20})
    assert r.status_code == 400 and r.json()["error"] == "This camera does not offer long shutter times over the cable."


def test_practice_picture_is_saved_as_a_new_file(studio):
    c, cam, st, handoff = studio
    b = io.BytesIO()
    Image.new("RGB", (1024, 576), (10, 10, 10)).save(b, "JPEG")
    r = c.post("/api/play/paint/save", content=b.getvalue(), headers={"Content-Type": "image/jpeg"}).json()
    assert r["ok"] and "PLAYGROUND › Paint with light" in r["say"]
    shoot = Path(handoff.current()["path"])
    assert len(list((shoot / "PLAYGROUND" / "Paint with light").glob("Practice *.jpg"))) == 1
    bad = c.post("/api/play/paint/save", content=b"not a picture", headers={"Content-Type": "image/jpeg"})
    assert bad.status_code == 400


# ---------------------------------------------------------------- shoot for a mood (Claude stubbed)
def test_mood_values_are_clamped_and_said(studio, monkeypatch):
    from camctl import assistant
    c, cam, st, handoff = studio
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    seen = []
    monkeypatch.setattr(assistant, "mood", lambda frame, words, ranges: seen.append(ranges) or {
        "aperture_f": 1.2, "shutter_s": 1 / 16000, "iso": 50, "kelvin": 20000, "guide": "space left",
        "light": "One lamp high on the left.", "room": ["Close the curtain"], "cannot": "No wider aperture."})
    r = c.post("/api/play/mood", json={"words": "film noir"}).json()
    assert r["clamped"] and "nearest" in r["note"]
    vals = {x["name"]: x["value"] for x in r["changes"]}
    assert vals.get("iso", 100) >= 100 and vals["color_temp"] <= 9900
    assert seen[0]["iso"][0] >= 100 and seen[0]["fixed_shutter_s"] is None
    before = cam.p[0x500A].value if 0x500A in cam.p else None                         # nothing applied by asking
    assert cam.p[0x500A].value == before


def test_mood_bad_answers_are_calm(studio, monkeypatch):
    from camctl import assistant
    c, cam, st, handoff = studio
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "sk-fake")
    for data in ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "{not json"}]},
                 {"stop_reason": "refusal", "content": []},
                 {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"aperture_f": "wide"})}]}):
        monkeypatch.setattr(assistant, "call", lambda body, key, data=data: data)
        r = c.post("/api/play/mood", json={"words": "silhouette"})
        assert r.status_code >= 400 and "Traceback" not in r.text and "Claude" in r.json()["error"]


def test_mood_prompt_forbids_looks():
    from camctl import assistant
    assert "Never suggest a colour look, a LUT" in assistant.MOOD_SYSTEM and "grading" in assistant.MOOD_SYSTEM


# ---------------------------------------------------------------- focus pull
def test_focus_pull_even_steps_and_never_records(studio):
    c, cam, st, handoff = studio
    cam.focus_at_moves = True
    a = c.post("/api/play/pull/mark", json={"which": "a", "x": 0.2, "y": 0.5}).json()
    b = c.post("/api/play/pull/mark", json={"which": "b", "x": 0.8, "y": 0.5}).json()
    assert b["pull"]["a"]["pos"] == 20 and b["pull"]["b"]["pos"] == 80
    cam.focus_steps = []
    c.post("/api/play/pull/run", json={"direction": "ab", "seconds": 1})
    for _ in range(100):
        if not c.get("/api/play").json()["pull"]["busy"]:
            break
        time.sleep(0.1)
    s = c.get("/api/play").json()["pull"]
    assert cam.focus_pos == 80 and "position 80" in s["say"] and "B" in s["say"]
    steps = [x for x in cam.focus_steps if x > 0]
    assert steps and max(steps) - min(steps) <= 1                                     # small even steps
    assert getattr(cam, "recordings_started", 0) == 0


def test_pull_plan():
    steps, gap = playground.pull_plan(10, 90, 2.0)
    assert sum(steps) == 80 and len(steps) == 40 and gap == pytest.approx(0.05)
    steps, gap = playground.pull_plan(50, 47, 1.0)
    assert steps == [-1, -1, -1]


# ---------------------------------------------------------------- one home
def test_experiments_live_only_in_the_playground():
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    for t in ('pxCard("Double exposure"', 'pxCard("Paint with light"', 'Shoot for a mood`', 'pxCard("Focus pull"'):
        assert page.count(t) == 1, t
    assert page.count('p.id="pxPill"') == 1 and page.count('g.id="pxGhost"') == 1
    assert "This is a small picture (1024 x 576) made from the live picture" in page
    assert "Not yet checked on the real camera" in page
    assert "Studio never starts or stops a recording for this." in page
