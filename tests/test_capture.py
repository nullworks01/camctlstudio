"""Capture profiles: neutral colour only, RAW / S-Log3 always, exposure by measurement, verdict."""
import pytest
from starlette.testclient import TestClient

from camctl import capture, presets, webui
from camctl.drivers.sony import PROPS, PropInfo

from test_autopilot import StubCam, _studio_with

COLOUR = {presets.PROP_WB_SHIFT_AB, presets.PROP_WB_SHIFT_GM, capture.PROP_PICTURE_EFFECT, 0xD201}


def _cam():
    cam = StubCam(scene_ev=-1)
    e = lambda code, val, ch, dt=4: PropInfo(code, dt, True, True, val, choices=list(ch))
    cam.p[0xD253] = e(0xD253, 3, [2, 3])                             # still format: RAW+JPEG / JPEG
    cam.p[0xD25F] = e(0xD25F, 1, [1, 3, 4])
    cam.p[0xD203] = e(0xD203, 2, [1, 2, 3])
    cam.p[capture.PROP_PICTURE_EFFECT] = e(capture.PROP_PICTURE_EFFECT, 0x8001, [0x8000, 0x8001, 0x8002])
    cam.p[0xD20F] = PropInfo(0xD20F, 4, True, True, 5600, range=(2500, 9900, 100))
    cam.p[0xD22C] = e(0xD22C, 259, [1, 2, 259])
    cam.p[0xD201] = e(0xD201, 31, [1, 31, 17, 18])                    # DRO Auto -> must go Off
    writes = []
    orig = cam.set

    def rec(prop, value):
        code = PROPS.get(prop, prop) if isinstance(prop, str) else prop
        writes.append((code, value))
        orig(prop, value)
    cam.set = rec
    return cam, writes


@pytest.mark.parametrize("key", capture.ORDER)
@pytest.mark.parametrize("mode", ["photo", "video"])
def test_profiles_never_write_colour_and_keep_raw_slog(monkeypatch, tmp_path, key, mode):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam, writes = _cam()
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/preset", json={"preset": key, "mode": mode}).json()
    assert "verdict" in r and r["look"] is None
    for code, v in writes:
        if code in COLOUR:
            assert v == capture.COLOUR_PROPS.get(code), f"{key}/{mode} wrote colour property 0x{code:04X}={v}"
        if code == presets.PROP_PICTURE_PROFILE:
            assert v == (8 if mode == "video" else 0)
        if code == 0xD253:
            assert v == 2                                         # never JPEG-only
        if code == PROPS["white_balance"]:
            assert v in (capture.WB_AUTO, capture.WB_KELVIN)
    assert cam.p[presets.PROP_WB_SHIFT_AB].value == 192 and cam.p[presets.PROP_WB_SHIFT_GM].value == 192
    assert cam.p[capture.PROP_PICTURE_EFFECT].value == 0x8000 and cam.p[0xD201].value == 1
    if mode == "photo":
        assert cam.p[0xD253].value == 2
        fmt = next(i for i in r["verdict"]["items"] if i["key"] == "format")
        assert fmt["status"] == "ok" and "RAW" in fmt["text"]
    else:
        assert cam.p[presets.PROP_PICTURE_PROFILE].value == 8
        assert next(i for i in r["verdict"]["items"] if i["key"] == "format")["text"].startswith("S-Log3")


def test_noise_guard_and_verdict_says_add_light(monkeypatch, tmp_path):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam, writes = _cam()
    cam.scene_ev = -7                                             # a very dark room
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/preset", json={"preset": "interview", "mode": "photo"}).json()
    assert cam.p[PROPS["iso"]].value <= capture.ISO_CEILING["photo"]  # exposure first, up to the stated ceiling
    ll = r["verdict"]["lowlight"]
    assert ll and "RAW at a higher ISO" in ll["strategy"] and "light" in r["verdict"]["todo"].lower()


def test_director_view_has_no_colour_properties():
    st = _studio_with(_cam()[0])
    view = webui.director_settings_view(st.props())
    names = {s["name"] for s in view}
    assert not names & {"wb_shift_ab", "wb_shift_gm", "0xD21B", "0xD201"}
    pp = next(s for s in view if s["name"] == "picture_profile")
    assert {c["value"] for c in pp["choices"]} <= {0, 8}


def test_exposure_first_raises_iso_and_states_the_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam, writes = _cam()
    cam.scene_ev = -2.5                                           # a dim room (reachable below the ceiling)
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/preset", json={"preset": "interview", "mode": "photo"}).json()
    items = {i["key"]: i for i in r["verdict"]["items"]}
    assert items["exposure"]["status"] == "ok", items                # correct exposure reached...
    assert cam.p[PROPS["iso"]].value > capture.NOISE_GUARD["photo"]  # ...by raising ISO past the old guard
    assert items["noise"]["status"] == "warn" and "One lamp" in items["noise"]["todo"]

def test_tap_focus_acquires_then_locks_and_labels_honestly(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import webui, measure, presets
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam = ZVE10(ev=9, person=False, mode="photo")
    cam.p[PROPS["focus_mode"]].value = 1                      # locked in MF before the tap
    monkeypatch.setattr(measure, "_faces_cv", lambda im: [])
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/focus/point", json={"x": 0.8, "y": 0.3}).json()
        assert r["locking"] is True                           # the answer comes first; the lock follows in the background
        import time as _t
        for _ in range(60):
            f = c.get("/api/focus").json()
            if not f.get("locking"):
                break
            _t.sleep(0.1)
        assert not f.get("locking")
        if r["found"]:
            assert f["text"].endswith(", locked")             # "Locked" only once the camera reads back MF
    assert cam.focused == (0.8, 0.3)                          # the camera was asked to focus there (not skipped in MF)
    assert cam.p[PROPS["focus_mode"]].value == 1              # and locked again afterwards
    assert r["target"] == "where you tapped" and "found" in r and "ms" in r
    if not r["found"]:
        assert r["why"]                                       # never a silent lock
