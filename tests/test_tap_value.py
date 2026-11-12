"""His tap on a number (aperture, shutter, ISO, white balance) reaches the camera and is read back; when the camera
cannot take it, the answer says why in plain words (12 Nov 2026: the tray posted to a route that did not exist,
so every tap showed "The camera didn't take it")."""
import re
from pathlib import Path

import pytest

from camctl.drivers.sony import PROPS

PAGE = Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html"


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
    cam = ZVE10(ev=10, mode="photo")
    cam.mem["photo"][PROPS["aperture"]].value = 350
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam


def test_every_api_path_the_page_calls_exists():
    from camctl import webui
    src = Path(webui.__file__).read_text(encoding="utf-8")
    routes = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r) + "$") for r in re.findall(r'Route\("(/api/[^"]+)"', src)]
    page = PAGE.read_text(encoding="utf-8")
    for p in set(re.findall(r'(?:post|api)\("(/api/[A-Za-z0-9_/\-\.]+)"', page)) - {"/api/liveview.jpg"} | {"/api/liveview.jpg"}:
        assert any(r.match(p) or r.match(p.rstrip("/") + "/x") for r in routes), p


def test_taps_on_the_four_numbers_are_taken_and_read_back(studio):
    c, cam = studio
    for name, value in (("aperture", 400), ("shutter_speed", 65636), ("iso", 1600), ("color_temp", 4000)):
        r = c.post("/api/set", json={"name": name, "value": value})
        assert r.status_code == 200 and r.json()["value"] == value, (name, r.text)
    r = c.post("/api/set", json={"name": "white_balance", "value": 4})
    assert r.status_code == 200, r.text


def test_a_value_the_camera_does_not_offer_says_so(studio):
    c, cam = studio
    r = c.post("/api/set", json={"name": "iso", "value": 7})
    assert r.status_code == 409, r.text; assert "not available" in r.json()["error"]


def test_the_lens_limit_is_said_in_words(studio):
    c, cam = studio
    cam.zoom_pos = 100.0                                   # zoomed all the way in: the lens cannot open to f/3.5
    cam._zoom_tick()
    r = c.post("/api/set", json={"name": "aperture", "value": 350})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "The lens cannot open wider than f/5.6 at this zoom."


def test_no_tap_while_the_lens_is_zooming(studio):
    c, cam = studio
    c.post("/api/zoom", json={"action": "start", "dir": 1})
    try:
        r = c.post("/api/set", json={"name": "iso", "value": 1600})
        assert r.status_code == 409 and "zooming" in r.json()["error"]
    finally:
        c.post("/api/zoom", json={"action": "stop"})


def test_a_slow_mode_change_is_not_reported_as_refused(studio):
    """12 Nov 2026: the camera switched Photo -> Movie M but its read-back lagged; Studio said 409 although it worked."""
    import threading
    c, cam = studio
    real_set = cam.set

    def slow_set(prop, value):
        if prop == PROPS["exposure_mode"] or prop == "exposure_mode":
            threading.Timer(3.5, lambda: real_set(prop, value)).start()   # the camera takes 3.5 s to report it
            return 0x2001
        return real_set(prop, value)
    cam.set = slow_set
    r = c.post("/api/set", json={"name": "exposure_mode", "value": 0x78053})
    assert r.status_code == 200, r.text
