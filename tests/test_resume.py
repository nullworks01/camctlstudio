"""Saved setups that pick a stopped shoot up again: what is stored, recall (exact values, zoom back, Locked, no Claude),
undo, the different-light sentence, the distance estimate and old setups. Simulator only."""
import base64
import io
import json
import time

import pytest
from PIL import Image

from camctl import resume
from camctl.drivers.sony import PROPS


# -- distance maths (known cases) ---------------------------------------------------------------------------------
def test_distance_known_cases():
    # a 0.15 m wide thing filling 10% of the width at 50 mm on a 23.5 mm wide sensor: 50 * 0.15 / 2.35 = 3.19 m
    assert abs(resume.distance_m(0.10, 50, 23.5, 0.15) - 3.19) < 0.01
    # the same face at 16 mm: 16 * 0.15 / 2.35 = 1.02 m
    assert abs(resume.distance_m(0.10, 16, 23.5, 0.15) - 1.02) < 0.01
    # an object 40 cm wide over half the picture at 30 mm: 30 * 0.4 / 11.75 = 1.02 m
    assert abs(resume.distance_m(0.5, 30, 23.5, 0.40) - 1.02) < 0.01


def _jpeg(color=(120, 110, 100), size=(1024, 576)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


def test_distance_not_known_without_face_size_or_focal_length():
    assert resume.estimate(_jpeg(), 16.0, 23.5, lambda im: [])["m"] is None
    assert resume.estimate(_jpeg(), None, 23.5, lambda im: [(0, 0, 100, 100)])["m"] is None     # no lens calibration
    assert resume.estimate(_jpeg(), 16.0, None, lambda im: [(0, 0, 100, 100)])["m"] is None     # unknown sensor
    e = resume.estimate(_jpeg(), 16.0, 23.5, lambda im: [(400, 100, 102, 140)])
    assert e["m"] == pytest.approx(16 * resume.FACE_WIDTH_M / (102 / 1024 * 23.5), abs=0.01) and "face" in e["method"]
    o = resume.estimate(_jpeg(), 16.0, 23.5, None, object_cm=40, object_box=[0.2, 0.3, 0.25, 0.3])
    assert o["m"] == pytest.approx(16 * 0.4 / (0.25 * 23.5), abs=0.01) and "object" in o["method"]


def test_light_sentence_only_beyond_one_stop():
    assert resume.light_sentence(0.6) == ""
    assert "1.4 stops brighter" not in resume.light_sentence(1.2) and "brighter" in resume.light_sentence(1.2)
    assert "darker" in resume.light_sentence(-2.0) and "Nothing was changed" in resume.light_sentence(-2.0)
    a, b = resume.light_level(_jpeg((60, 60, 60))), resume.light_level(_jpeg((120, 120, 120)))
    assert 1.5 < resume.stops_between(a, b) < 3.0                  # twice the code value is about 2.1 stops of light


def test_outline_is_lines_on_transparent():
    b = io.BytesIO()
    im = Image.new("RGB", (640, 360), (40, 40, 40))
    im.paste((220, 220, 220), (200, 100, 440, 260))
    im.save(b, "JPEG")
    png = Image.open(io.BytesIO(resume.outline_png(b.getvalue())))
    assert png.mode == "RGBA"
    a = png.getchannel("A")
    assert a.getextrema() == (0, 235) and sum(1 for v in a.tobytes() if v) < 0.1 * png.width * png.height


def test_line_up_instructions():
    assert resume.line_up_say({"scores": {"framing": 92}, "raw": {}}) == ("Lined up", True)
    assert resume.line_up_say({"scores": {"framing": 60}, "raw": {"scale": 1.3, "dx": 0, "dy": 0, "cc": 0.7}})[0] == "Further back"
    assert resume.line_up_say({"scores": {"framing": 60}, "raw": {"scale": 0.8, "dx": 0, "dy": 0, "cc": 0.7}})[0] == "Move closer"
    assert resume.line_up_say({"scores": {"framing": 60}, "raw": {"scale": 1.0, "dx": -0.1, "dy": 0, "cc": 0.7}})[0] == "A little left"
    assert resume.line_up_say({"scores": {"framing": 60}, "raw": {"scale": 1.0, "dx": 0, "dy": -0.1, "cc": 0.7}})[0] == "Camera higher"


# -- the whole flow on the simulator ------------------------------------------------------------------------------
@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import assistant, handoff, setups, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(setups, "SETUP_DIR", tmp_path / "setups")
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    monkeypatch.setattr(assistant, "_post", lambda *a, **k: pytest.fail("a Claude call was made"))
    cam = ZVE10(ev=10, mode="photo")
    cam.mem["photo"][PROPS["aperture"]].value = 350
    from camctl import measure
    monkeypatch.setattr(measure, "_faces_cv", lambda im: cam.face_box_px(im.width, im.height))   # the simulated face
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, st, tmp_path


def _vals(c):
    return {s["name"]: s["value"] for s in c.get("/api/settings").json()["settings"]}


def test_save_holds_what_a_stopped_shoot_needs(studio):
    c, cam, st, tmp = studio
    c.post("/api/set", json={"name": "iso", "value": 800})
    r = c.post("/api/setups/save", json={"name": "Test setup", "notes": "window left",
                                         "measure": {"subject_m": 1.5, "height_m": "1.2", "lights": [{"name": "Key", "dist_m": 1.1, "height_m": ""}]}}).json()
    d = c.get(f"/api/setups/{r['id']}").json()
    assert d["v"] == 2 and d["mode"] == "photo" and d["notes"] == "window left"
    assert {s["name"]: s["value"] for s in d["settings"]}["iso"] == 800
    assert d["zoom"]["pos"] == 0 and d["zoom"]["mm"] == 16
    assert Image.open(io.BytesIO(base64.b64decode(d["frame"]))).size == Image.open(io.BytesIO(st.frame)).size   # the clean camera picture
    assert Image.open(io.BytesIO(base64.b64decode(d["outline"]))).mode == "RGBA"
    assert d["light"]["level"] > 0 and d["camera"] == "ZV-E10"
    assert d["distance"]["m"] and d["distance"]["focal_mm"] == 16.0 and "face" in d["distance"]["method"]
    assert d["measure"] == {"subject_m": 1.5, "height_m": 1.2, "lights": [{"name": "Key", "dist_m": 1.1, "height_m": None}]}
    raw = json.dumps(d)
    assert "sk-ant" not in raw and "key" not in {k.lower() for k in d}                   # nothing secret


def test_recall_exact_values_zoom_back_locked_then_undo(studio):
    c, cam, st, tmp = studio
    c.post("/api/set", json={"name": "iso", "value": 800})
    sid = c.post("/api/setups/save", json={"name": "Test setup"}).json()["id"]
    c.post("/api/set", json={"name": "iso", "value": 3200})
    cam.zoom_pos = 40.0; cam._zoom_tick()
    before = _vals(c)
    r = c.post(f"/api/setups/{sid}/recall").json()
    assert r["all_back"] and r["read_back"]["iso"] and _vals(c)["iso"] == 800
    assert abs(r["zoom"]["now"] - 0) <= 3 and r["zoom"]["before"] == 40
    assert r["state"] == "Locked: Test setup" and r["focus"] == "Not checked" and r["light_say"] == ""
    assert c.get("/api/focus").json()["text"] == "not checked after the recall"
    u = c.post("/api/setups/undo").json()
    assert u["ok"] and _vals(c)["iso"] == before["iso"] and abs(cam.zoom_pos - 40) <= 4
    assert c.post("/api/setups/undo").status_code == 409                                  # once


def test_clearly_different_light_is_said_and_nothing_changes_for_it(studio):
    c, cam, st, tmp = studio
    sid = c.post("/api/setups/save", json={"name": "Test setup"}).json()["id"]
    saved = _vals(c)
    cam.ev += 2.0                                                                         # two stops more light now
    time.sleep(0.5)
    r = c.post(f"/api/setups/{sid}/recall").json()
    assert "brighter" in r["light_say"] and r["light_stops"] > 1.0
    after = _vals(c)
    assert all(after[k] == saved[k] for k in ("aperture", "shutter_speed", "iso"))        # only said, not corrected


def test_an_old_setup_still_loads_and_recalls(studio):
    c, cam, st, tmp = studio
    from camctl import setups
    setups.SETUP_DIR.mkdir(parents=True, exist_ok=True)
    old = {"id": "a1b2c3d4e5f6", "name": "Old one", "notes": "", "mode": "photo", "created": 1.0,
           "settings": [{"name": "iso", "value": 1600, "label": "1600", "friendly": ""}], "preset": None, "look": None,
           "plan": None, "refs": [], "thumb": None}
    (setups.SETUP_DIR / "a1b2c3d4e5f6.json").write_text(json.dumps(old), encoding="utf-8")
    lst = c.get("/api/setups").json()
    assert any(x["id"] == "a1b2c3d4e5f6" and x["zoom_mm"] is None for x in lst)
    r = c.post("/api/setups/a1b2c3d4e5f6/recall").json()
    assert r["all_back"] and _vals(c)["iso"] == 1600 and r["zoom"]["moved"] is False and r["light_say"] == ""


def test_object_size_gives_a_distance_after_saving(studio):
    c, cam, st, tmp = studio
    sid = c.post("/api/setups/save", json={"name": "Product"}).json()["id"]
    d = c.post(f"/api/setups/{sid}/update", json={"object": {"cm": 30, "box": [0.3, 0.3, 0.2, 0.3]}}).json()
    assert d["distance"]["m"] == pytest.approx(16 * 0.30 / (0.2 * 23.5), abs=0.01) and "object" in d["distance"]["method"]


def test_the_ghost_is_only_on_the_monitor(studio):
    """While Line up is on, a saved picture, the live picture Claude would get and the camera's frames stay clean."""
    c, cam, st, tmp = studio
    import numpy as np
    sid = c.post("/api/setups/save", json={"name": "Ghost source"}).json()["id"]
    on = c.post("/api/lineup", json={"setup": sid}).json()
    assert on["on"] and on["outline"].startswith("data:image/png") and on["picture"].startswith("data:image/jpeg")
    assert c.get("/api/lineup").json()["say"]
    sid2 = c.post("/api/setups/save", json={"name": "While ghost on"}).json()["id"]
    saved = np.asarray(Image.open(io.BytesIO(base64.b64decode(c.get(f"/api/setups/{sid2}").json()["frame"]))).convert("L"), np.float32)
    live = np.asarray(Image.open(io.BytesIO(c.get("/api/liveview.jpg").content)).convert("L"), np.float32)
    assert saved.shape == live.shape and abs(saved - live).mean() < 6          # the camera picture, no lines on it
    assert (saved > 250).mean() < 0.01                                          # no white outline pixels
    page = (__import__("pathlib").Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert "luGhost" in page and "toDataURL" not in page.split("function luGhost")[1].split("function luStart")[0]
    assert c.post("/api/lineup", json={}).json()["on"] is False
