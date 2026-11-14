"""Formats: every format the camera offers can be chosen (All settings › Camera › Format), Studio's choice stays the
default, and the Operator exposes for the format in use. All on the simulator (no real camera)."""
import time

import pytest


@pytest.fixture
def studio(monkeypatch, tmp_path):
    yield from _make(monkeypatch, tmp_path, None)


@pytest.fixture
def sim_c(monkeypatch, tmp_path):
    yield from _make(monkeypatch, tmp_path, "SIM-C")


def _make(monkeypatch, tmp_path, model):
    from starlette.testclient import TestClient
    from camctl import assistant, handoff, measure, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui.setups, "SETUP_DIR", tmp_path / "setups")
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    calls = []
    monkeypatch.setattr(assistant, "has_key", lambda: False)
    monkeypatch.setattr(assistant, "ask", lambda *a, **k: calls.append(a) or {})
    cam = ZVE10(ev=9, mode="photo", model=model)
    monkeypatch.setattr(measure, "_faces_cv", lambda im: cam.face_box_px(im.width, im.height))
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        c.claude_calls = calls
        yield c, cam, st


def _switch(c, m):
    c.post("/api/set", json={"name": "exposure_mode", "value": 491603 if m == "video" else 1})
    return c.post("/api/post", json={"post": True, "mode": m, "setup": True}).json()


def _vals(c):
    return {s["name"]: s for s in c.get("/api/settings").json()["settings"]}


def _row(d, mode, key):
    return next(r for r in d[mode] if r["key"] == key)


# ---------------------------------------------------------------- Studio's choice is today's behaviour
def test_studio_choice_is_unchanged_raw_jpeg_and_slog3_4k25(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    assert _vals(c)["still_format"]["value"] == 2                       # RAW + JPEG
    _switch(c, "video")
    v = _vals(c)
    assert v["picture_profile"]["value"] == 8 and v["movie_setting"]["value"] == 0x21   # S-Log3, 4K 25p 100M
    assert v["shutter_speed"]["label"] == "1/50s"
    s = c.get("/api/settings").json()["format"]
    assert s["name"].startswith("S-Log3") and "4K · 25p · 100\u00a0Mbps" in s["name"]


def test_rows_offer_what_the_camera_reports_with_names(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    d = c.get("/api/formats").json()
    assert d["active"] == "photo"
    assert [x["label"] for x in _row(d, "photo", "still_format")["choices"]] == ["RAW", "RAW + JPEG", "JPEG"]
    assert {x["label"] for x in _row(d, "photo", "aspect")["choices"]} == {"3:2", "16:9", "4:3", "1:1"}
    pp = {x["value"]: x["label"] for x in _row(d, "photo", "picture_profile")["choices"]}
    assert pp[0] == "Off (standard)" and "S-Log3" in pp[8] and "S-Log2" in pp[7] and "HLG" in pp[10]
    assert all(r["studio"] for r in d["photo"])                       # every row has its "Studio's choice"
    _switch(c, "video")
    d = c.get("/api/formats").json()
    assert {x["label"] for x in _row(d, "video", "movie_format")["choices"]} == {"4K video (XAVC S)", "HD video (XAVC S)"}
    assert _row(d, "photo", "still_format")["active"] is False         # the other mode: the lists seen last time
    assert _row(d, "photo", "still_format")["choices"]


# ---------------------------------------------------------------- his choice is respected
def test_jpeg_only_is_kept_and_exposed_carefully(studio):
    from camctl import capture
    c, cam, _ = studio
    _switch(c, "photo")
    r = c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": 3}).json()
    assert r["ok"] and "JPEG" in r["say"]
    assert cam.p[0xD253].value == 3
    v = _switch(c, "photo")["verdict"]                                   # a new setup keeps his JPEG
    assert cam.p[0xD253].value == 3
    assert any(i["key"] == "format" and i["status"] == "ok" and i["text"].startswith("JPEG ✓") for i in v["items"])
    assert any(i["key"] == "jpeg" and "no RAW" in i["text"] for i in v["items"])
    assert c.get("/api/settings").json()["format"]["jpeg_only"] is True
    assert capture.target_level(False, True, True, jpeg_only=True) < capture.target_level(False, True, True)
    # the pre-take check enforces his format, not RAW + JPEG
    cam.p[0xD253].value = 2
    c.post("/api/shoot", json={})
    assert cam.p[0xD253].value == 3


def test_hlg_video_uses_the_cameras_iso_floor_and_a_normal_picture(studio):
    c, cam, _ = studio
    _switch(c, "video")
    r = c.post("/api/formats", json={"mode": "video", "key": "picture_profile", "value": 10}).json()
    assert r["ok"] and r.get("setup", "").startswith("Set up again for HLG")
    v = _vals(c)
    assert v["picture_profile"]["value"] == 10
    from camctl.drivers.sony import PROPS
    iso_floor = min(x for x in cam.p[PROPS["iso"]].choices if x < 0xFFFFFF)
    assert iso_floor == 100                                               # read from the camera, not 500
    s = c.get("/api/settings").json()
    assert s["log"]["active"] is False and s["format"]["pp_kind"] == "hlg"   # no LOG | 709 switch, no log targets
    assert not c.claude_calls                                             # built-in rules only
    ev = c.get("/api/autofix").json()
    assert any("set up again for HLG" in e["text"] for e in ev["events"])
    assert ev["phase"] == "locked"
    hist = c.get("/api/director/status").json()["log"]
    assert any(e.get("undoable") and "Clean capture" in e.get("title", e.get("text", "")) for e in hist) or \
        any(e.get("undoable") for e in hist)


def test_frame_rate_choice_sets_the_shutter(studio):
    from camctl import capture
    c, cam, _ = studio
    _switch(c, "video")
    r = c.post("/api/formats", json={"mode": "video", "key": "movie_format", "value": 9}).json()
    assert r["ok"]
    d = r
    rates = {x["label"]: x["value"] for x in _row(d, "video", "movie_setting")["choices"]}
    assert "HD · 50p · 50\u00a0Mbps" in rates and "HD · 100p · 100\u00a0Mbps" in rates
    r = c.post("/api/formats", json={"mode": "video", "key": "movie_setting", "value": rates["HD · 50p · 50\u00a0Mbps"]}).json()
    assert r["ok"]
    assert _vals(c)["shutter_speed"]["label"] == "1/100s"               # 50p under 50 Hz mains
    # the rule itself, for every rate
    assert capture.video_shutter(25, 50)[0] == pytest.approx(1 / 50)
    assert capture.video_shutter(30, 50)[0] == pytest.approx(1 / 50)
    assert capture.video_shutter(60, 50)[0] == pytest.approx(1 / 100)
    assert capture.video_shutter(100, 50)[0] == pytest.approx(1 / 100)
    assert capture.video_shutter(24, 60)[0] == pytest.approx(1 / 40)
    assert capture.video_shutter(25, 60)[0] == pytest.approx(1 / 60)
    assert capture.video_shutter(50, 60)[0] == pytest.approx(1 / 120)
    assert capture.video_shutter(120, 60)[0] == pytest.approx(1 / 120)
    t, safe = capture.video_shutter(120, 50)
    assert t == pytest.approx(1 / 240) and safe is False


def test_a_refused_format_keeps_the_previous_value_calmly(studio):
    from camctl import formats
    c, cam, _ = studio
    _switch(c, "photo")
    cam.refuse[0xD211] = {4}                                              # the camera says "not now" to 1:1
    r = c.post("/api/formats", json={"mode": "photo", "key": "aspect", "value": 4}).json()
    assert r["ok"] is False
    assert "stays on 16:9" in r["say"] and "Error" not in r["say"] and "0x" not in r["say"]
    assert cam.p[0xD211].value == 2
    assert formats.chosen("photo", "aspect") is None                      # nothing stored


def test_a_value_the_camera_does_not_offer_is_refused(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    r = c.post("/api/formats", json={"mode": "photo", "key": "aspect", "value": 9}).json()
    assert r["ok"] is False and "doesn't offer" in r["say"]


def test_each_mode_keeps_its_own_format(studio):
    c, cam, _ = studio
    _switch(c, "video")                                                    # Studio has seen video's lists once
    _switch(c, "photo")
    c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": 1})            # RAW only
    r = c.post("/api/formats", json={"mode": "video", "key": "picture_profile", "value": 0}).json()   # not active
    assert r["ok"] and "when you switch to Video" in r["say"]
    assert cam.mem["video"][0xD23F].value == 8                             # nothing written to video yet
    _switch(c, "video")
    assert _vals(c)["picture_profile"]["value"] == 0
    _switch(c, "photo")
    assert _vals(c)["still_format"]["value"] == 1
    r = c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": None}).json()   # Studio's choice
    assert r["ok"] and "Studio's choice" in r["say"] and _vals(c)["still_format"]["value"] == 2


def test_aspect_choice_and_reset_leaves_the_camera_alone(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    assert c.post("/api/formats", json={"mode": "photo", "key": "aspect", "value": 1}).json()["ok"]
    assert cam.p[0xD211].value == 1
    s = c.get("/api/settings").json()["format"]
    assert s["ratio_name"] == "3:2" and s["ratio"] == pytest.approx(1.5)
    _switch(c, "photo")
    assert cam.p[0xD211].value == 1                                       # kept by the next setup
    c.post("/api/formats", json={"mode": "photo", "key": "aspect", "value": None})
    assert cam.p[0xD211].value == 1                                       # Studio's choice: as the camera has it


def test_not_while_recording(studio):
    c, cam, st = studio
    st.recording = True
    try:
        r = c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": 3})
        assert r.status_code == 409 and r.json()["error"] == "Stop the recording to change the format."
    finally:
        st.recording = False


def test_saved_setup_stores_and_recalls_the_format(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": 3})
    sid = c.post("/api/setups/save", json={"name": "jpeg look", "mode": "photo"}).json()["id"]
    assert any(s.get("format", "").startswith("JPEG") for s in c.get("/api/setups").json())
    c.post("/api/formats", json={"mode": "photo", "key": "still_format", "value": None})
    assert cam.p[0xD253].value == 2
    r = c.post(f"/api/setups/{sid}/recall", json={})
    assert r.status_code == 200, r.text
    assert cam.p[0xD253].value == 3


def test_camera_report_lists_the_formats(studio, tmp_path):
    c, cam, _ = studio
    _switch(c, "photo")
    r = c.post("/api/camera/report", json={}).json()
    assert "Formats the camera offers:" in r["summary"]
    assert "Photo file: RAW, RAW + JPEG, JPEG" in r["summary"]


def test_slog2_targets_sit_lower_than_slog3():
    from camctl import capture
    assert capture.target_level(True, True, False, "slog2") < capture.target_level(True, True, False, "slog3") - 15


# ---------------------------------------------------------------- an invented camera with other formats
def test_other_camera_offers_its_own_formats(sim_c):
    c, cam, _ = sim_c
    _switch(c, "photo")
    d = c.get("/api/formats").json()
    sf = _row(d, "photo", "still_format")
    assert [x["label"] for x in sf["choices"]] == ["RAW + JPEG", "JPEG"]
    asp = _row(d, "photo", "aspect")
    assert asp["settable"] is False and asp["note"] == "Set this in the camera menu." and asp["value_name"] == "3:2"
    pp = [x["label"] for x in _row(d, "photo", "picture_profile")["choices"]]
    assert pp == ["Off (standard)", "Profile 1", "Profile 2", "Profile 3"]     # unknown slots named plainly
    r = c.post("/api/formats", json={"mode": "photo", "key": "aspect", "value": 2}).json()
    assert r["ok"] is False
    _switch(c, "video")
    d = c.get("/api/formats").json()
    assert [x["label"] for x in _row(d, "video", "movie_format")["choices"]] == ["HD video (XAVC S)"]
    rates = [x["label"] for x in _row(d, "video", "movie_setting")["choices"]]
    assert "HD · 60p · 50\u00a0Mbps" in rates and not any("4K" in x for x in rates)


# ---------------------------------------------------------------- tidy: one home, order, his JPEG quality / size kept
def test_rows_read_in_the_order_a_person_thinks(studio):
    c, cam, _ = studio
    _switch(c, "video")
    _switch(c, "photo")
    d = c.get("/api/formats").json()
    assert [r["key"] for r in d["photo"]] == ["still_format", "jpeg_quality", "image_size", "aspect", "picture_profile"]
    assert [r["key"] for r in d["video"]] == ["movie_format", "movie_setting", "picture_profile"]
    assert all(r["value_name"] for r in d["photo"] + d["video"])             # the current value at a glance


def test_jpeg_quality_and_size_follow_his_pick(studio):
    c, cam, _ = studio
    _switch(c, "photo")
    assert cam.p[0xD25F].value == 4 and cam.p[0xD203].value == 1             # Studio's choice: best, largest
    assert c.post("/api/formats", json={"mode": "photo", "key": "jpeg_quality", "value": 3}).json()["ok"]
    assert c.post("/api/formats", json={"mode": "photo", "key": "image_size", "value": 2}).json()["ok"]
    _switch(c, "photo")                                                        # a new setup does not force them back
    assert cam.p[0xD25F].value == 3 and cam.p[0xD203].value == 2
    c.post("/api/formats", json={"mode": "photo", "key": "image_size", "value": None})
    assert cam.p[0xD203].value == 1


def test_old_photo_preview_switch_becomes_the_profile_row(studio):
    from camctl import formats
    c, cam, _ = studio
    _switch(c, "photo")
    c.post("/api/post", json={"photo_log": True})
    d = c.get("/api/formats").json()
    assert formats.chosen("photo", "picture_profile") == 8
    assert c.get("/api/post").json().get("photo_log") is False
    row = _row(d, "photo", "picture_profile")
    assert row["note"] == "Photos look flat like your log video; the RAW is not affected."


def test_page_has_one_home_for_formats():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert 'id="photoLog"' not in page and 'data-sw="photolog"' not in page   # the old switch is gone
    assert page.count('asSeg("jpeg_quality"') == 0 and page.count('asSeg("image_size"') == 0
    assert page.count('asSeg("movie_setting"') == 0                          # only in Format now
    assert page.count('"/api/formats"') >= 2 and page.count('id="asFormat"') == 1
