"""Optional HDMI capture card, with ffmpeg's TEST PATTERN as the device (never a real webcam, never a picture of
anyone): nothing changes without a card, the picture source switches and falls back, recording on the laptop only by
his press with the true state, disk guard, device names never logged or reported."""
import time
from pathlib import Path

import pytest

from camctl import capture_card, video

FF = video.find_ffmpeg()
pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg not available")

SAMPLE = '''[dshow @ 0000] "Integrated Camera" (video)
[dshow @ 0000]   Alternative name "@device_pnp_x"
[dshow @ 0000] "Cam Link 4K" (video)
[dshow @ 0000] "Microphone Array" (audio)
[dshow @ 0000] "Cam Link 4K" (audio)
'''


def test_device_list_is_parsed_and_opens_nothing():
    d = capture_card.parse_devices(SAMPLE)
    assert d == {"video": ["Integrated Camera", "Cam Link 4K"], "audio": ["Microphone Array", "Cam Link 4K"]}


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
    monkeypatch.setattr(capture_card, "FAKE", True)
    cam = ZVE10(ev=9, mode="video")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, st, handoff
        if getattr(st, "card", None) is not None:
            st.card.stop()


def _frames(st, n=5, timeout=10):
    from PIL import Image
    import io
    end = time.time() + timeout
    size = None
    while time.time() < end:
        f, _ = st.latest_frame(-1, timeout=1)
        if f:
            size = Image.open(io.BytesIO(f)).size
            if size == (1280, 720):
                return size
        time.sleep(0.2)
    return size


def test_no_card_changes_nothing(studio, monkeypatch):
    c, cam, st, handoff = studio
    monkeypatch.setattr(capture_card, "FAKE", False)
    monkeypatch.setattr(capture_card.video, "find_ffmpeg", lambda: None)
    s = c.post("/api/card", json={"list": True}).json()
    assert s["source"] == "camera" and s["devices"]["ffmpeg"] is False and getattr(st, "card", None) is None
    r = c.post("/api/card", json={"source": "card", "video": "x"})
    assert r.status_code == 400 and "ffmpeg" in r.json()["error"]
    assert _frames(st, timeout=2) == (320, 180)                                       # still the camera's picture


def test_choose_the_card_then_unplug_falls_back(studio):
    c, cam, st, handoff = studio
    s = c.post("/api/card", json={"list": True}).json()
    assert s["devices"]["video"] == ["Test pattern"]
    s = c.post("/api/card", json={"source": "card", "video": "Test pattern", "audio": "Test tone"}).json()
    assert s["source"] == "card"
    assert _frames(st) == (1280, 720)                                                 # its own size, as it arrives
    assert st.frame is not None                                                       # analysis reads this picture
    time.sleep(1.0)
    assert c.get("/api/card").json()["level_db"] is not None                          # the card's sound meter
    st.card.proc.kill()                                                               # "unplugged"
    for _ in range(80):
        if getattr(st, "card", None) is None:
            break
        time.sleep(0.1)
    s = c.get("/api/card").json()
    assert s["source"] == "camera" and "stopped sending a picture" in s["note"]
    assert _frames(st, timeout=4) == (320, 180)


def test_record_on_the_laptop_only_by_press_with_the_true_state(studio):
    c, cam, st, handoff = studio
    c.post("/api/card", json={"list": True})
    c.post("/api/card", json={"source": "card", "video": "Test pattern", "audio": "Test tone"})
    _frames(st)
    shoot = Path(handoff.ensure_shoot()["path"])
    time.sleep(2.0)
    assert not list((shoot / "VIDEO").glob("*")) if (shoot / "VIDEO").exists() else True   # never by itself
    c.post("/api/card", json={"record_to": "laptop"})
    r = c.post("/api/record", json={"action": "start"}).json()
    assert r["state"] in ("starting", "recording") and r["say"] in ("Starting…", "Recording on the laptop")
    for _ in range(80):
        if c.get("/api/card").json()["rec"]["state"] == "recording":
            break
        time.sleep(0.1)
    assert c.get("/api/card").json()["rec"]["state"] == "recording" and st.recording is True
    assert c.get("/api/record/state").json()["recording"] is True
    time.sleep(2.5)
    c.post("/api/record", json={"action": "stop"})
    assert c.get("/api/card").json()["rec"]["state"] in ("finishing", "saved")
    for _ in range(150):
        if c.get("/api/card").json()["rec"]["state"] == "saved":
            break
        time.sleep(0.1)
    s = c.get("/api/card").json()
    assert s["rec"]["state"] == "saved" and "Saved" in s["rec"]["say"]
    mp4s = list((shoot / "VIDEO").glob("* laptop.mp4"))
    assert len(mp4s) == 1 and not list((shoot / "VIDEO").glob("*.mkv"))
    info = video.probe(mp4s[0])
    assert info["duration"] > 1.5 and info["audio"] and (info["width"], info["height"]) == (1280, 720)
    assert cam.recordings_started == 0                                                # the camera's own recording untouched
    assert s["source"] == "card"                                                      # the picture goes on


def test_disk_guard_refuses_a_start(studio, monkeypatch):
    c, cam, st, handoff = studio
    c.post("/api/card", json={"list": True})
    c.post("/api/card", json={"source": "card", "video": "Test pattern"})
    c.post("/api/card", json={"record_to": "laptop"})
    monkeypatch.setattr(capture_card, "disk_free", lambda p: 1_000_000_000)
    r = c.post("/api/record", json={"action": "start"})
    assert r.status_code == 409 and "less than 2 GB free" in r.json()["error"]
    assert c.get("/api/card").json()["rec"]["state"] == "idle"


def test_a_failed_conversion_keeps_the_mkv(tmp_path):
    mkv = tmp_path / "x laptop.mkv"
    mkv.write_bytes(b"not a video")
    out, say = capture_card.finish_file(mkv)
    assert out is None and mkv.exists() and "x laptop.mkv" in say and "VIDEO folder" in say


def test_device_names_never_logged_or_reported(studio):
    c, cam, st, handoff = studio
    c.post("/api/card", json={"list": True})
    c.post("/api/card", json={"source": "card", "video": "Test pattern", "audio": "Test tone"})
    ev = c.get("/api/autofix").json()["events"]
    assert any("capture card" in e["text"] for e in ev) and not any("Test pattern" in e["text"] for e in ev)
    rep = c.post("/api/camera/report", json={}).json()
    assert "Test pattern" not in rep["summary"] and "capture" not in rep["summary"].lower()


def test_one_home_and_honest_words():
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert page.count("<h4>Picture source</h4>") == 1 and page.count("data-recto=") == 2
    assert "Not yet tested on a real capture card" in page
    assert "The picture pauses for about a second when a recording on the laptop starts and stops." in page
    assert "Whether the ZV-E10 keeps sending HDMI while PC Remote is on is not known yet." in page
    src = Path(capture_card.__file__).read_text(encoding="utf-8")
    assert "Integrated" not in src and "webcam" in src.lower()                         # never opened by name in code
