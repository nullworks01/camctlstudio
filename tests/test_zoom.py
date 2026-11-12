"""Power zoom: only on his press; position and about-millimetres; the light it costs is said; focus counts as unchecked."""
import time

import pytest


def test_mm_table_round_trip():
    from camctl.drivers.sony import zoom_mm, zoom_pos_for
    assert zoom_mm(0) == 16 and zoom_mm(100) == 50
    for mm in (16, 20, 24, 28, 35, 50):
        assert abs(zoom_mm(zoom_pos_for(mm)) - mm) <= 0.7


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
    from camctl.drivers.sony import PROPS
    cam.mem["photo"][PROPS["aperture"]].value = 350
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam


def test_hold_closer_then_stop_says_the_light_and_unchecks_focus(studio):
    c, cam = studio
    z = c.get("/api/zoom").json()
    assert z["available"] and z["pos"] == 0 and z["mm"] == 16
    assert c.post("/api/zoom", json={"action": "start", "dir": 1}).json()["moving"] == 1
    time.sleep(1.2)
    r = c.post("/api/zoom", json={"action": "stop"}).json()["result"]
    assert 40 <= r["pos"] <= 80 and r["mm"] > 25
    assert r["f_before"] == 3.5 and r["f"] > 3.5 and r["darker_stops"] > 0.5      # the lens closed down by itself
    assert c.get("/api/focus").json()["text"] == "not checked after the zoom"
    assert cam.zoom_dir == 0                                                    # stopped


def test_goto_about_35_mm_and_back_to_wide(studio):
    c, cam = studio
    r = c.post("/api/zoom", json={"action": "goto", "mm": 35}).json()["result"]
    assert abs(r["mm"] - 35) <= 3, r
    r = c.post("/api/zoom", json={"action": "goto", "mm": 16}).json()["result"]
    assert r["pos"] == 0 and r["mm"] == 16 and r["f"] == 3.5
    assert cam.zoom_dir == 0


def test_a_lost_stop_never_leaves_the_lens_running(studio):
    c, cam = studio
    c.post("/api/zoom", json={"action": "start", "dir": 1})
    time.sleep(6.8)
    assert cam.zoom_dir == 0 and c.get("/api/zoom").json()["moving"] == 0


def test_no_zoom_while_studio_sets_up_the_camera(studio):
    """Verified on the real camera: a zoom during the setup on connect made its exposure loop stop down to f/22."""
    c, cam = studio
    from camctl import webui  # noqa: F401
    st = c.app.state if hasattr(c.app, "state") else None  # noqa: F841
    import gc
    s = next(o for o in gc.get_objects() if type(o).__name__ == "Studio" and getattr(o, "cam", None) is cam)
    s.setup_active = 1
    r = c.post("/api/zoom", json={"action": "start", "dir": 1})
    assert r.status_code == 409 and "setting up" in r.json()["error"]
    assert cam.zoom_dir == 0
    s.setup_active = 0
