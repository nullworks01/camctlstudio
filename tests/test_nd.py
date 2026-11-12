"""His variable ND on the lens: the switch, the live reading for turning the ring, no 'light changed' proposals while it
is on, the ISO-limit sentence, saved setups with the ND. Simulator only; nothing here writes to the camera."""
import gc
import time

import pytest

from camctl import autofix, resume
from camctl.drivers.sony import PROPS


def test_reading_maths_known_ratios():
    # twice / half the light at the same values = +/- 1 stop
    import io
    from PIL import Image

    def lvl(v):
        b = io.BytesIO(); Image.new("RGB", (64, 36), (v, v, v)).save(b, "JPEG"); return resume.light_level(b.getvalue())
    mid = lvl(118)
    assert abs(resume.stops_between(mid, lvl(162)) - 1.0) < 0.15      # sRGB 118 -> 162 is about one stop more light
    assert abs(resume.stops_between(mid, lvl(84)) + 1.0) < 0.15


@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, setups, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(setups, "SETUP_DIR", tmp_path / "setups")
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    monkeypatch.setattr(webui, "PREFS", tmp_path / "prefs.json", raising=False)
    cam = ZVE10(ev=10, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        op = next(o for o in gc.get_objects() if isinstance(o, autofix.AutoFix) and o.studio is st)
        op.baseline = {"t": time.time() - 10, "ire": None}
        op.phase = "locked"
        yield c, cam, st, op
        c.post("/api/nd", json={"on": False})


def _settle(st, n=3):
    _, seq = st.latest_frame(-1, timeout=2)
    for _ in range(n):
        _, seq = st.latest_frame(seq, timeout=2)


def _read(c, k=8):
    r = None
    for _ in range(k):                       # smoothed: a few readings settle it
        r = c.get("/api/nd").json()
        time.sleep(0.15)
    return r


def test_switch_on_then_filter_reads_too_dark_and_its_strength(studio):
    c, cam, st, op = studio
    _settle(st)
    assert _read(c)["text"] == "Right"                 # the aim: the picture after Studio's setup
    c.post("/api/nd", json={"on": True})               # switched on before the filter goes on: the reference
    cam.ev -= 2.0                                      # the filter takes two stops
    _settle(st)
    r = _read(c, 12)
    assert r["on"] and "too dark" in r["text"] and 1.4 < -r["stops"] < 2.6
    assert r["strength"] and 1.4 < r["strength"] < 2.6 and r["strength_text"].startswith("The filter is taking about")


def test_strength_not_known_without_a_reference(studio):
    c, cam, st, op = studio
    c.post("/api/nd", json={"on": True})
    c.post("/api/set", json={"name": "iso", "value": 1600})   # other values than at the switch: no clean reference
    _settle(st)
    r = _read(c)
    assert r["strength"] is None and r["strength_text"] == "The filter's strength is not known yet."


def test_no_light_change_proposal_while_the_nd_is_on(monkeypatch):
    from test_autofix import _op
    op, st, decided, applied = _op(monkeypatch, [50, 50, 50, 20, 20, 20, 20, 20], prefs={"auto_apply": False, "nd_on": True})
    op.tick()
    for _ in range(7):
        op.tick()
    assert op.proposal is None and not applied


def test_at_the_iso_limit_it_says_turn_the_nd_lighter(studio):
    c, cam, st, op = studio
    _settle(st)
    _read(c)
    c.post("/api/nd", json={"on": True})
    c.post("/api/set", json={"name": "iso", "value": 12800})  # his photo ceiling
    op.baseline = {"t": time.time() - 10, "ire": None}
    _settle(st)
    _read(c)                                           # this exposure is the aim now ...
    cam.ev -= 2.5                                      # ... then the filter is turned darker
    _settle(st)
    r = _read(c, 12)
    assert "too dark" in r["text"] and r["limit"] == "ISO is at your limit: turn the ND lighter or add light."


def test_saved_with_the_nd_and_recalled(studio):
    c, cam, st, op = studio
    _settle(st)
    _read(c)
    c.post("/api/nd", json={"on": True})
    cam.ev -= 2.0
    _settle(st)
    _read(c, 12)
    sid = c.post("/api/setups/save", json={"name": "ND shoot"}).json()["id"]
    d = c.get(f"/api/setups/{sid}").json()
    assert d["nd"]["on"] and d["nd"]["variable"] and d["nd"]["strength"] and d["nd"]["strength"] > 1
    c.post("/api/nd", json={"on": False})
    cam.ev += 3.0                                       # filter off, brighter room: would be a light change otherwise
    _settle(st)
    r = c.post(f"/api/setups/{sid}/recall").json()
    assert r["nd_say"].startswith("This shoot used the ND at about") and r["light_say"] == ""
    rd = _read(c)
    assert rd["recall"] and "too bright" in rd["text"]   # guides the ring back to the saved brightness
    cam.ev -= 3.0
    _settle(st)
    assert _read(c, 14)["text"] == "Right"


def test_an_old_setup_without_the_nd_field(studio):
    c, cam, st, op = studio
    from camctl import setups
    import json
    setups.SETUP_DIR.mkdir(parents=True, exist_ok=True)
    old = {"id": "b1b2c3d4e5f6", "name": "Old", "notes": "", "mode": "photo", "created": 1.0,
           "settings": [{"name": "iso", "value": 800, "label": "800", "friendly": ""}], "preset": None, "look": None,
           "plan": None, "refs": [], "thumb": None}
    (setups.SETUP_DIR / "b1b2c3d4e5f6.json").write_text(json.dumps(old), encoding="utf-8")
    r = c.post("/api/setups/b1b2c3d4e5f6/recall").json()
    assert r["all_back"] and r["nd_say"] == ""
