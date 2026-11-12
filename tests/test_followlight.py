import time

from camctl import followlight as fl, presets, webui
from camctl.drivers.sony import PROPS
from test_autopilot import StubCam


def test_plan_step_rules():
    cam = StubCam()
    cam.p[PROPS["iso"]].value = 1600
    p = cam.refresh()
    assert fl.plan_step(p, 1.0, False, 6400, False) == ("iso", 3200)        # ISO first
    assert fl.plan_step(p, 1.0, True, 6400, False) == ("iso", 3200)         # one notch while recording
    assert fl.plan_step(p, 3.0, True, 6400, False) == ("iso", 3200)         # never jumps 2 stops on air
    assert fl.plan_step(p, 0.45, False, 6400, False) == (None, "closest")  # a 1-stop move would overshoot
    cam.p[PROPS["iso"]].value = 6400
    assert fl.plan_step(cam.refresh(), 1.0, False, 6400, True) == (None, "too_dark")   # cap + iris locked
    cam.p[PROPS["iso"]].value = 100
    cam.p[PROPS["aperture"]].value = 1100
    assert fl.plan_step(cam.refresh(), -2, False, 6400, False) == (None, "too_bright")


def test_follows_light_change_in_slog(monkeypatch):
    monkeypatch.setattr(fl, "HOLD_S", 0.1)
    monkeypatch.setattr(fl, "SETTLE_S", 0.1)
    monkeypatch.setattr(fl, "TICK_S", 0.02)
    cam = StubCam(scene_ev=0)
    cam.p[PROPS["exposure_mode"]].value = presets.MODE_MOVIE_M
    cam.p[presets.PROP_PICTURE_PROFILE].value = 8
    cam.p[PROPS["iso"]].value = 800
    cam.p[PROPS["shutter_speed"]].value = (1 << 16) | 50
    st = webui.Studio("usb"); st.cam = cam
    f = fl.LightFollower(st)
    f.start({"over": 1.5, "max_iso": 6400})
    deadline = time.time() + 8
    while time.time() < deadline and not f.status["message"].startswith("✓"):
        time.sleep(0.05)
    assert f.status["message"].startswith("✓") and f.status["log"] and abs(f.status["error"]) < 0.6
    assert presets.shutter_seconds(cam.p[PROPS["shutter_speed"]].value) == 1 / 50   # never touched
    iso_lit = cam.p[PROPS["iso"]].value
    cam.scene_ev = -2                                     # a cloud / lights dimmed
    deadline = time.time() + 8
    time.sleep(0.5)
    while time.time() < deadline and cam.p[PROPS["iso"]].value <= iso_lit:
        time.sleep(0.05)
    assert cam.p[PROPS["iso"]].value > iso_lit
    cam.scene_ev = 9                                      # walked into sunlight
    deadline = time.time() + 10
    while time.time() < deadline and "ND" not in f.status["message"]:
        time.sleep(0.05)
    assert "ND" in f.status["message"]
    f.stop()
