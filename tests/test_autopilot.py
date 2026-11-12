"""Autopilot, noise guard, verified presets and the live-view pump (no camera needed)."""
import io
import math
import time

from PIL import Image

from camctl import presets, webui
from camctl.drivers.sony import PROPS, PropInfo

APS = [350, 400, 450, 500, 560, 630, 710, 800, 900, 1000, 1100]
SHS = [(1 << 16) | d for d in (4, 8, 15, 30, 50, 60, 100, 125, 250, 500, 1000, 2000)]
ISOS = [100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600, 0xFFFFFF]


class StubCam:
    """Simulated ZV-E10: live-view brightness follows the exposure settings."""
    name = "ZV-E10"

    def __init__(self, scene_ev=0.0):
        self.scene_ev = scene_ev      # 0 = metered at f/3.5 1/60 ISO 800 gives mid grey
        self.frames = 0
        e = lambda code, val, ch, dt=6: PropInfo(code, dt, True, True, val, choices=list(ch))
        self.p = {
            PROPS["aperture"]: e(PROPS["aperture"], 350, APS, 4),
            PROPS["shutter_speed"]: e(PROPS["shutter_speed"], (1 << 16) | 60, SHS),
            PROPS["iso"]: e(PROPS["iso"], 0xFFFFFF, ISOS),
            PROPS["exposure_mode"]: e(PROPS["exposure_mode"], 65538, [1, 65538, 131075, 196612, 0x78053]),
            PROPS["focus_mode"]: e(PROPS["focus_mode"], 1, [1, 2, 32772], 4),
            PROPS["white_balance"]: e(PROPS["white_balance"], 4, [2, 4, 6, 32786], 4),
            PROPS["drive_mode"]: e(PROPS["drive_mode"], 0x8013, [1, 0x8013], 4),
            presets.PROP_PICTURE_PROFILE: e(presets.PROP_PICTURE_PROFILE, 8, range(0, 11), 2),   # stuck on S-Log3
        }
        for code in (presets.PROP_WB_SHIFT_AB, presets.PROP_WB_SHIFT_GM):
            self.p[code] = PropInfo(code, 4, True, True, 200, range=(164, 220, 1))

    def refresh(self):
        return dict(self.p)

    def set(self, prop, value):
        code = PROPS.get(prop, prop) if isinstance(prop, str) else prop
        self.p[code].value = value

    def live_view(self):
        self.frames += 1
        t = presets.shutter_seconds(self.p[PROPS["shutter_speed"]].value) or 1 / 60
        iso = self.p[PROPS["iso"]].value
        iso = 800 if iso == 0xFFFFFF else iso
        n = self.p[PROPS["aperture"]].value / 100
        stops = math.log2((t * iso / n ** 2) / ((1 / 60) * 800 / 3.5 ** 2)) + self.scene_ev
        level = 255 * min(max((118 / 255) ** 2.2 * 2 ** stops, 0), 1) ** (1 / 2.2)
        buf = io.BytesIO()
        im = Image.new("RGB", (64, 36), (int(level),) * 3)
        im.putpixel((self.frames % 64, 0), (255, 0, 0))     # sensor noise: every real frame differs
        im.save(buf, "JPEG")
        return buf.getvalue()


def test_dark_room_respects_noise_guard_and_resets_slog(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    cam = StubCam(scene_ev=-5)                      # very dark room
    r = presets.apply_preset(cam, "portrait")
    assert cam.p[PROPS["iso"]].value <= 3200        # default photo cap, never 12800 grain
    assert cam.p[presets.PROP_PICTURE_PROFILE].value == 0   # S-Log left over from before is gone
    assert cam.p[PROPS["drive_mode"]].value == 1
    assert cam.p[PROPS["exposure_mode"]].value == 1
    assert r["max_iso"] == 3200 and r["verified"] >= 5 and r["mismatch"] == []
    assert "warning" in r                            # tells the user to add light


def test_cinematic_without_look_uses_normal_colour(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    cam = StubCam(scene_ev=0)
    r = presets.apply_preset(cam, "cinematic", max_iso=1600)
    assert cam.p[presets.PROP_PICTURE_PROFILE].value == 0
    assert presets.shutter_seconds(cam.p[PROPS["shutter_speed"]].value) == 1 / 50
    assert cam.p[PROPS["iso"]].value <= 1600 and r["mismatch"] == []


def test_mismatch_is_reported(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    cam = StubCam()
    real_set = cam.set
    cam.set = lambda prop, v: None if prop == "focus_mode" else real_set(prop, v)   # camera ignores it
    r = presets.apply_preset(cam, "portrait")
    assert any("focus" in m for m in r["mismatch"])


def _studio_with(cam):
    st = webui.Studio("usb")
    st.cam = cam
    return st


def test_pump_skips_repeated_frames():
    cam = StubCam()
    same = cam.live_view()
    cam.live_view = lambda: same
    st = _studio_with(cam)
    _, s1 = st.latest_frame(-1, timeout=2)
    time.sleep(0.5)
    _, s2 = st.latest_frame(s1, timeout=0.3)
    assert s1 == s2 == 1          # the same picture again is not a new frame


def test_live_view_pump_serves_fresh_frames():
    cam = StubCam()
    st = _studio_with(cam)
    f1, s1 = st.latest_frame(-1, timeout=3)
    assert f1 and f1[:2] == b"\xff\xd8" and s1 >= 1
    f2, s2 = st.latest_frame(s1, timeout=3)
    assert s2 > s1
    time.sleep(0.3)
    assert st.fps > 1


def test_autopilot_endpoint_builtin(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.assistant, "has_key", lambda: False)
    cam = StubCam(scene_ev=-2)
    app = webui.build_app(_studio_with(cam))
    with TestClient(app) as c:
        r = c.post("/api/autopilot", json={"mode": "photo", "intent": "auto"})
        assert r.status_code == 200, r.text
        j = r.json()
        from camctl import capture
        assert j["autopilot"]["by"] == "auto" and j["autopilot"]["preset"] in capture.PROFILES
        assert cam.p[PROPS["iso"]].value <= capture.ISO_CEILING["photo"] and j["verdict"]["items"]   # exposure-first ceiling
        r = c.post("/api/autopilot", json={"mode": "video", "intent": "interview"})
        assert r.status_code == 200 and r.json()["autopilot"]["preset"] == "interview"
        assert c.post("/api/autopilot", json={"mode": "video", "intent": "nonsense"}).status_code == 400
        lv = c.get("/api/liveview.jpg?after=-1")
        assert lv.status_code == 200 and lv.headers["x-frame-seq"]


def test_legacy_preset_keys_map_to_capture_profiles(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    app = webui.build_app(_studio_with(StubCam()))
    with TestClient(app) as c:
        j = c.post("/api/autopilot", json={"mode": "photo", "intent": "landscape"}).json()
        assert j["autopilot"]["preset"] == "daylight" and j["preset"] == "Daylight exterior" and j["look"] is None
