"""Recording can never double-start (12 Nov 2026: the camera's REC flag read "not recording" 8 s into a take, Studio
believed it, and the stop press went out as a start). The simulated camera's movie button is a toggle and its flag
is flaky; Studio must keep its own intent and follow the camera only on strong evidence (flag AND card time)."""
import time

import pytest


@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, setups, video, webui
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    monkeypatch.setattr(video, "capture_devices", lambda: [])
    cam = ZVE10(ev=10, mode="video")
    cam.rec_flaky = [(0.3, 1.0), (7.5, 3.0)]          # wrong flag right after start and about 8 s in
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, st


def _poll(c, secs, every=0.5):
    out = []
    end = time.time() + secs
    while time.time() < end:
        out.append(c.get("/api/record/state").json())
        time.sleep(every)
    return out


def test_one_press_one_recording_and_the_flaky_flag_changes_nothing(studio):
    c, cam, st = studio
    r = c.post("/api/record", json={"action": "start"}).json()
    assert r["sent"] and r["recording"] and cam.recordings_started == 1
    states = _poll(c, 11.0)
    assert all(s["recording"] for s in states)                      # the 8 s flag glitch never flipped it
    r = c.post("/api/record", json={"action": "stop"}).json()
    assert r["sent"] and r["recording"] is False
    assert cam.recordings_started == 1 and not cam.recording           # exactly one take, and it is stopped


def test_a_stop_press_never_starts(studio):
    c, cam, st = studio
    r = c.post("/api/record", json={"action": "stop"}).json()
    assert r["sent"] is False and not getattr(cam, "recording", False) and cam.recordings_started == 0


def test_a_start_while_recording_sends_nothing(studio):
    c, cam, st = studio
    c.post("/api/record", json={"action": "start"})
    time.sleep(2.6)
    r = c.post("/api/record", json={"action": "start"}).json()
    assert r["sent"] is False and cam.recording and cam.recordings_started == 1
    time.sleep(0.1)
    c.post("/api/record", json={"action": "stop"})


def test_uncertain_state_blocks_a_start_then_follows_strong_evidence(studio):
    c, cam, st = studio
    cam.rec_flaky = []
    cam.movie_record(True)                                             # started with the button on the camera itself
    s = _poll(c, 2.0)[-1]
    assert s["checking"] and s["recording"] is False
    r = c.post("/api/record", json={"action": "start"})
    assert r.status_code == 409 and "Checking the camera" in r.json()["error"]
    assert cam.recordings_started == 1 and cam.recording                # nothing was sent: still that one take
    s = _poll(c, 7.0)[-1]                                              # flag AND card time agree for 6 s
    assert s["recording"] is True and s["checking"] is False and "started on the camera" in s["note"]
    c.post("/api/record", json={"action": "stop"})
    assert not cam.recording


def test_the_camera_stopping_by_itself_is_followed_only_on_evidence(studio):
    c, cam, st = studio
    cam.rec_flaky = []
    c.post("/api/record", json={"action": "start"})
    time.sleep(3.2)
    cam.movie_record(True)                                             # the camera stops on its own (card full, warm)
    s = _poll(c, 2.0)[-1]
    assert s["recording"] is True and s["checking"]                    # not yet: a flag alone is not evidence
    s = _poll(c, 7.0)[-1]
    assert s["recording"] is False and "stopped recording by itself" in s["note"]
    assert cam.recordings_started == 1                                 # Studio never sent anything to "correct" it


def test_settings_report_studio_intent_not_the_flaky_flag(studio):
    c, cam, st = studio
    c.post("/api/record", json={"action": "start"})
    time.sleep(7.8)                                                    # inside the 7.5 s flag glitch
    assert c.get("/api/settings").json()["recording"] is True
    c.post("/api/record", json={"action": "stop"})
