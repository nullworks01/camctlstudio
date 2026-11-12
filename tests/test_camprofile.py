"""Other Sony cameras: Studio works from what the camera reports. The ZV-E10 keeps its measured profile;
an unknown model is 'untested', no picture profile is assumed to be log, and 'Check this camera' finds out without
shooting. SIM-A / SIM-FX / SIM-B are INVENTED simulated models, not real cameras."""
import time

import pytest

from camctl import assistant, camprofile, presets


def test_zve10_profile_is_todays_values():
    p = camprofile.for_model("ZV-E10 (simulated)")
    assert p["builtin"] and p["tested"] and p["log_pp"] == 8 and p["s1_settle_s"] == 3.2
    assert camprofile.is_log(p, 8) and camprofile.is_log(p, 7) and not camprofile.is_log(p, 0)
    assert camprofile.log_kind(p, 8) == "slog3" and camprofile.log_kind(p, 7) == "slog2"


def test_unknown_model_assumes_nothing():
    p = camprofile.for_model("ILCE-9999")
    assert not p["tested"] and p["log_pp"] is None and not camprofile.is_log(p, 8)
    assert p["noise_guard"] == {"photo": 3200, "video": 6400} and "noise_guard" in p["defaults"]   # labelled defaults
    assert p["lens"] is None                                                                       # no mm without a calibration


@pytest.fixture
def make(monkeypatch, tmp_path):
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
    clients = []

    def _make(model=None, mode="photo"):
        cam = ZVE10(ev=10, mode=mode, model=model)
        st = _studio_with(cam)
        c = TestClient(webui.build_app(st))
        c.__enter__()
        clients.append(c)
        st.latest_frame(-1, timeout=3)
        return c, cam, st
    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def _switch(c, m):
    c.post("/api/set", json={"name": "exposure_mode", "value": 491603 if m == "video" else 1})
    return c.post("/api/post", json={"post": True, "mode": m, "setup": True}).json()


def test_untested_model_never_writes_a_picture_profile(make):
    c, cam, st = make("SIM-A")
    s = c.get("/api/settings").json()
    assert s["profile"]["model"] == "SIM-A" and s["profile"]["tested"] is False
    before = cam.mem["video"][presets.PROP_PICTURE_PROFILE].value
    r = _switch(c, "video")
    assert cam.p[presets.PROP_PICTURE_PROFILE].value == before                       # left as it was
    assert any(i["text"] == "Log profile not confirmed on this model" for i in r["verdict"]["items"])
    assert assistant.CAMERA_NAME == "Sony SIM-A"                                      # prompts name the real camera


def test_check_this_camera_finds_the_log_profile_and_he_confirms(make):
    c, cam, st = make("SIM-A")
    assert c.post("/api/camera/check").json()["ok"]
    for _ in range(300):
        s = c.get("/api/camera/check").json()
        if not s["running"] and s["done"]:
            break
        time.sleep(0.1)
    names = {x["name"]: x["ok"] for x in s["steps"]}
    assert names["Connected"] and names["Live picture"] and names["Aperture"] and names["ISO"]
    assert names["Power zoom"] is None                                                # this model has none
    assert [x["pp"] for x in s["log_candidates"]] == [7]                              # PP7 looks like log here
    assert cam.p[presets.PROP_PICTURE_PROFILE].value == 0                             # the original one is back
    assert s["profile"]["tested"] is True
    c.post("/api/camera/log", json={"pp": 7})
    _switch(c, "video")
    assert cam.p[presets.PROP_PICTURE_PROFILE].value == 7                             # used only after his OK


def test_no_power_zoom_no_millimetres(make):
    c, cam, st = make("SIM-B")
    z = c.get("/api/zoom").json()
    assert z["available"] is False
    r = c.post("/api/zoom", json={"action": "goto", "mm": 28})
    assert r.status_code == 400


def test_zoom_without_a_calibration_shows_the_position_only(make):
    c, cam, st = make("SIM-FX")
    z = c.get("/api/zoom").json()
    assert z["available"] is True and z["mm"] is None
    assert c.post("/api/zoom", json={"action": "goto", "mm": 28}).status_code == 400


def test_model_without_picture_profiles(make):
    c, cam, st = make("SIM-FX")
    r = _switch(c, "video")
    assert r["verdict"]                                                               # the setup runs
    assert presets.PROP_PICTURE_PROFILE not in cam.p


def test_sony_on_the_cable_without_a_driver_is_said(monkeypatch):
    from camctl import webui
    import camctl.ptpusb as ptpusb
    monkeypatch.setattr(ptpusb, "sony_without_driver", lambda: [{"name": "ILCE-7M4", "class": "WPD", "instance": "x"}])
    st = webui.Studio("usb")
    st.released = False
    st.fail_since = time.time() - 10
    st._present_at, st._present_cache = time.time(), False
    h = st.health()
    assert h.get("no_driver") and "Windows has no driver" in h["message"] and "does not install" in h["message"]


def test_newer_sign_in_is_said(monkeypatch):
    from camctl import ptpip
    import socket

    def fake_connect(addr, timeout=None):
        if addr[1] == 22:
            class _S:
                def close(self):
                    pass
            return _S()
        raise ConnectionRefusedError()
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    cl = ptpip.PTPIPClient.__new__(ptpip.PTPIPClient)
    cl.host, cl.port, cl.timeout = "10.0.0.99", 15740, 1.0
    with pytest.raises(Exception, match="sign-in"):
        cl.connect()
