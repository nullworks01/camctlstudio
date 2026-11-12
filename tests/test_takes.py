"""Takes log, verified card import matched to takes, shot list, take review + continuity."""
import os
import time

from starlette.testclient import TestClient

from camctl import handoff, importer, presets, shotplan, webui
from camctl.drivers.sony import PROPS

from test_autopilot import StubCam, _studio_with


def _shoot(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "Shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    return handoff.new_shoot("Test")


def test_card_import_copies_verifies_and_matches(monkeypatch, tmp_path):
    sh = _shoot(monkeypatch, tmp_path)
    t1 = importer.log_take("video", time.time() - 60, time.time() - 40, "Opening hook")
    t2 = importer.log_take("video", time.time() - 20, time.time() - 5, "Main talk")
    card = tmp_path / "card"
    clipdir = card / "PRIVATE" / "M4ROOT" / "CLIP"
    clipdir.mkdir(parents=True)
    offset = 3600.0                                     # the camera clock is an hour ahead
    for name, t in (("C0001.MP4", t1), ("C0002.MP4", t2)):
        f = clipdir / name
        f.write_bytes(os.urandom(5000))
        os.utime(f, (t["end"] + offset, t["end"] + offset))
    monkeypatch.setattr(importer, "find_card", lambda: card)
    got = []
    imp = importer.Importer(on_imported=lambda d, take: got.append((d.name, take and take["shot"])))
    imp.scan_once()
    assert sorted(got) == [("C0001.MP4", "Opening hook"), ("C0002.MP4", "Main talk")]
    assert (clipdir / "C0001.MP4").exists()                                   # card untouched
    dst = handoff.SHOOTS.joinpath(sh["path"]).joinpath("VIDEO", "C0001.MP4")
    assert dst.read_bytes() == (clipdir / "C0001.MP4").read_bytes()
    assert all(t["imported"] for t in importer.takes())
    got.clear()
    imp.scan_once()                                                           # nothing new: no copies
    assert got == [] and imp.status["waiting"] == []


def test_plan_template_tick_and_review_continuity(monkeypatch, tmp_path):
    _shoot(monkeypatch, tmp_path)
    monkeypatch.setattr(shotplan.assistant, "has_key", lambda: False)
    p = shotplan.make_plan("YouTube intro", "video")
    assert p["by"] == "template" and len(p["shots"]) == 4 and p["current"] == 0
    t = importer.log_take("video", 0, 1, p["shots"][0]["name"])
    shotplan.tick(t)
    assert shotplan.load()["shots"][0]["done"]
    prev = [{"id": 1, "measure": {"ire": 55, "cast": 0.02, "values": {"white_balance": 32786, "color_temp": 4600}}}]
    now = {"ire": 45, "cast": 0.09, "values": {"white_balance": 2, "color_temp": 5600}}
    r = shotplan.local_review({"checks": [{"key": "focus", "status": "ok", "label": "sharp"}]}, now, prev)
    assert not r["continuity"]["matches"] and len(r["continuity"]["notes"]) == 3
    assert shotplan.match_changes(prev[0], now["values"]) == [{"name": "white_balance", "value": 32786},
                                                              {"name": "color_temp", "value": 4600}]


def test_endpoints_plan_setup_photo_take_review(monkeypatch, tmp_path):
    _shoot(monkeypatch, tmp_path)
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.assistant, "has_key", lambda: False)
    monkeypatch.setattr(shotplan.assistant, "has_key", lambda: False)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    cam = StubCam()
    st = _studio_with(cam)
    cam.capture = lambda af=True: [(0x3801, cam.live_view())]
    app = webui.build_app(st)
    with TestClient(app) as c:
        p = c.post("/api/plan", json={"description": "portraits", "mode": "photo"}).json()
        assert p["shots"][0]["name"] == "Hero portrait"
        r = c.post("/api/plan/setup", json={"index": 0}).json()
        assert r["via"] == "auto"
        st.latest_frame(-1, timeout=3)
        j = c.post("/api/shoot").json()
        assert j["take"] == 1
        t = c.get("/api/takes").json()["takes"][0]
        assert t["shot"] == "Hero portrait" and t["imported"] and "review" in t
        assert c.get("/api/plan").json()["shots"][0]["done"]


def test_old_clips_from_before_the_shoot_are_left_alone(monkeypatch, tmp_path):
    sh = _shoot(monkeypatch, tmp_path)
    t = importer.log_take("video", time.time() - 30, time.time() - 10, "A")
    clipdir = tmp_path / "card" / "PRIVATE" / "M4ROOT" / "CLIP"
    clipdir.mkdir(parents=True)
    for name, when in (("C0001.MP4", sh["created"] - 5 * 3600), ("C0002.MP4", t["end"])):
        f = clipdir / name
        f.write_bytes(b"x" * 100)
        os.utime(f, (when, when))
    monkeypatch.setattr(importer, "find_card", lambda: tmp_path / "card")
    got = []
    importer.Importer(on_imported=lambda d, take: got.append(d.name)).scan_once()
    assert got == ["C0002.MP4"]
