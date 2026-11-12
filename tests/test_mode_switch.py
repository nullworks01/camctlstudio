"""Photo -> Video -> Photo: the photo picture comes back (profile off, photo exposure), also when he taps fast."""
import threading
import time

import pytest


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
        yield c, cam


def _switch(c, m):
    c.post("/api/set", json={"name": "exposure_mode", "value": 491603 if m == "video" else 1})
    return c.post("/api/post", json={"post": True, "mode": m, "setup": True}).json()


def _vals(c):
    return {s["name"]: s for s in c.get("/api/settings").json()["settings"]}


def test_photo_video_photo_in_order(studio):
    c, cam = studio
    _switch(c, "photo")
    photo_shutter = _vals(c)["shutter_speed"]["value"]
    _switch(c, "video")
    v = _vals(c)
    assert v["picture_profile"]["value"] == 8 and v["exposure_mode"]["value"] == 491603
    _switch(c, "photo")
    p = _vals(c)
    assert p["picture_profile"]["value"] == 0                     # the normal picture again
    assert p["exposure_mode"]["value"] == 1
    assert p["still_format"]["value"] == 2                        # RAW + JPEG
    assert p["shutter_speed"]["label"] != "1/50s" or photo_shutter == p["shutter_speed"]["value"]   # no video shutter left


def test_fast_taps_never_overlap_and_the_last_one_wins(studio, monkeypatch):
    from camctl import webui
    c, cam = studio
    _switch(c, "photo")
    running, worst = [0], [0]
    real = webui.capture.CaptureRun.formats

    def counted(self, mode):
        running[0] += 1
        worst[0] = max(worst[0], running[0])
        try:
            time.sleep(0.3)
            return real(self, mode)
        finally:
            running[0] -= 1
    monkeypatch.setattr(webui.capture.CaptureRun, "formats", counted)
    out = {}
    t1 = threading.Thread(target=lambda: out.setdefault("v", _switch(c, "video")))
    t1.start()
    time.sleep(0.15)
    t2 = threading.Thread(target=lambda: out.setdefault("p1", _switch(c, "photo")))
    t3 = threading.Thread(target=lambda: out.setdefault("p2", _switch(c, "photo")))
    t2.start(); time.sleep(0.05); t3.start()
    for t in (t1, t2, t3):
        t.join(60)
    assert worst[0] == 1                                          # never two setups writing at the same time
    p = _vals(c)
    assert p["exposure_mode"]["value"] == 1 and p["picture_profile"]["value"] == 0


def test_video_settings_are_never_written_in_photo_mode(studio):
    """A video setup that finds the camera in photo mode writes nothing video-like (the grey picture)."""
    from camctl import capture, webui
    from camctl.drivers.sony import PROPS
    c, cam = studio
    _switch(c, "photo")
    st = webui.Studio  # noqa: F841
    run = capture.CaptureRun.__new__(capture.CaptureRun)
    run.props = lambda: {**cam.p}
    writes = []
    run.set = lambda code, v: writes.append((code, v))
    run.intended, run.log = {}, (lambda *a: None)
    run.label = lambda n, v: str(v)
    cam.p[PROPS["exposure_mode"]].value = 1                       # he is back in photo
    run._want = lambda code, value: (writes.append((code, value)) or True) if code != PROPS["exposure_mode"] else True
    notes = capture.CaptureRun.formats(run, "video")
    assert not any(code == 0xD23F for code, _ in writes), writes   # no S-Log3 written into photo
    assert any("not in movie mode" in n for n in notes)
